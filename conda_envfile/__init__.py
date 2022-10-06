import argparse
import os
import re
import sys
import warnings

import click
import packaging.version
import yaml
from jinja2 import BaseLoader
from jinja2 import Environment

from ._version import version


def _parse(version):
    return packaging.version.parse(version)


def condaforge_dependencies(
    text: str,
    name: str = None,
    flatten: bool = True,
    selectors: list[str] = [],
    target_platform: str = "myplatform",
) -> list[str]:
    """
    Get the dependencies from a conda-forge feedstock.

    :param name: Name of the recipe to select (use to select one of multi-outputs).
    :param flatten: Flatten the dependencies, otherwise keep as ``"host"``, ``"run"``, ``"build"``.
    :param selectors: List of selectors to keep (all non-selected selectors are removed).
    :param target_platform: Target platform to use to substitute ``{{ target_platform }}``.
    """

    data = text.replace("{{ compiler('c') }}", "c-compiler")
    data = data.replace("{{ compiler('cxx') }}", "cxx-compiler")
    data = data.replace('{{ compiler("c") }}', "c-compiler")
    data = data.replace('{{ compiler("cxx") }}', "cxx-compiler")
    rtemplate = Environment(loader=BaseLoader).from_string(data)
    data = rtemplate.render(target_platform=target_platform)

    data = data.split("\n")

    rm_selectors = [
        "x86",
        "x86_64",
        "linux",
        "linux32",
        "linux64",
        "armv6l",
        "armv7l",
        "aarch64",
        "ppc64le",
        "osx",
        "arm64",
        "unix",
        "win",
        "win32",
        "win64",
        "py",
        "py3k",
        "py2k",
        "py27",
        "py34",
        "py35",
        "py36",
        "np",
        "build_platform",
        "build_platform != target_platform",
    ]

    for selector in selectors:
        rm_selectors.remove(selector)

    for selector in rm_selectors:
        data = [i for i in data if not re.match(rf"(.*)(# \[{selector}\])(.*)", i)]

    data = yaml.load("\n".join(data), Loader=yaml.FullLoader)

    ret = {key: [] for key in ["host", "run", "build"]}

    if "outputs" in data:
        for sub in data["outputs"]:
            if not name or sub["name"] == name:
                for key in ["host", "run", "build"]:
                    if key in sub["requirements"]:
                        ret[key] = sub["requirements"][key]
    else:
        for key in ret:
            if key in data["requirements"]:
                ret[key] = data["requirements"][key]

    if flatten:
        out = []
        for key in ret:
            out += ret[key]
        return unique(*out)

    return ret


def parse_file(*args: list[str]) -> dict:
    """
    Parse one or more files and return the raw result.

    :param args: List of filenames to parse.
    :return: Raw result.
    """

    env = {"name": [], "channels": [], "dependencies": []}

    for filename in args:

        if not os.path.isfile(filename):
            raise FileNotFoundError(filename)

        with open(filename) as file:

            data = yaml.load(file.read(), Loader=yaml.FullLoader)

            for key, value in data.items():
                if key not in env:
                    raise ValueError(f"Unknown key '{key}' in '{filename}'.")
                if type(value) == str:
                    env[key].append(value)
                elif type(value) == list:
                    env[key] += value

    for key in ["channels", "name"]:
        if "channels" in env:
            env["channels"] = list(set(env["channels"]))

    if len(env["name"]) > 1:
        raise ValueError("Multiple 'name' keys.")
    if len(env["name"]) == 1:
        env["name"] = env["name"][0]
    else:
        del env["name"]

    return env


def _check_legal(dep: dict):
    """
    Check that the dependency is legal, make small simplifications.
    """

    if "=" in dep:

        if "<" in dep:
            if _parse(dep["<"]) < _parse(dep["="]):
                raise ValueError(f"Invalid dependency: {dep}")
            del dep["<"]

        if "<=" in dep:
            if _parse(dep["<="]) < _parse(dep["="]):
                raise ValueError(f"Invalid dependency: {dep}")
            del dep["<="]

        if ">" in dep:
            if _parse(dep[">"]) > _parse(dep["="]):
                raise ValueError(f"Invalid dependency: {dep}")
            del dep[">"]

        if ">=" in dep:
            if _parse(dep[">="]) > _parse(dep["="]):
                raise ValueError(f"Invalid dependency: {dep}")
            del dep[">="]

        return dep

    if "<" in dep and ">" in dep:
        if _parse(dep[">"]) >= _parse(dep["<"]):
            raise ValueError(f"Invalid version range: {dep}")

    if "<" in dep and ">=" in dep:
        if _parse(dep[">="]) >= _parse(dep["<"]):
            raise ValueError(f"Invalid version range: {dep}")

    if "<=" in dep and ">" in dep:
        if _parse(dep[">"]) >= _parse(dep["<="]):
            raise ValueError(f"Invalid version range: {dep}")

    if "<=" in dep and ">=" in dep:
        if _parse(dep[">="]) > _parse(dep["<="]):
            raise ValueError(f"Invalid version range: {dep}")
        elif _parse(dep[">="]) == _parse(dep["<="]):
            dep["="] = dep[">="]
            for key in ["<=", ">=", "<", ">"]:
                if key in dep:
                    del dep[key]

    return dep


def _bounds(dep: dict) -> list[str]:
    """
    Return the bounds of the dependency.
    """

    if "=" in dep:
        return [_parse(dep["="]), _parse(dep["="])]

    ret = [_parse("0"), _parse("9999999999999999")]

    if ">=" in dep and ">" in dep:
        if _parse(dep[">="]) > _parse(dep[">"]):
            ret[0] = _parse(dep[">="])
        else:
            ret[0] = _parse(dep[">"])
    elif ">=" in dep:
        ret[0] = _parse(dep[">="])
    elif ">" in dep:
        ret[0] = _parse(dep[">"])

    if "<=" in dep and "<" in dep:
        if _parse(dep["<="]) < _parse(dep["<"]):
            ret[1] = _parse(dep["<="])
        else:
            ret[1] = _parse(dep["<"])
    elif "<=" in dep:
        ret[1] = _parse(dep["<="])
    elif "<" in dep:
        ret[1] = _parse(dep["<"])

    return ret


def _merge(*args) -> dict:
    """
    Merge two dependencies, keep the most restrictive dependencies.
    """
    assert len(args) == 2
    a = {**args[0]}
    b = {**args[1]}

    assert a["name"] == b["name"]

    if "special" in a and "special" in b:
        ba = _bounds(a)
        bb = _bounds(b)
        if ba[0] >= bb[0] and ba[1] <= bb[1]:
            return a
        elif bb[0] >= ba[0] and bb[1] <= ba[1]:
            return b
        else:
            del a["special"]
            del b["special"]

    if "=" in a and "=" in b:
        if _parse(a["="]) != _parse(b["="]):
            raise ValueError(f"Multiple version dependencies: {a['name']}")
        else:
            return a

    ret = {key: value for key, value in a.items()}

    if "special" in b:
        ret["special"] = b["special"]

    for key in b:
        if key not in ret:
            ret[key] = b[key]
        else:
            if key in ["<", "<="]:
                if _parse(b[key]) < _parse(ret[key]):
                    ret[key] = b[key]
            elif key in [">", ">="]:
                if _parse(b[key]) > _parse(ret[key]):
                    ret[key] = b[key]

    if "<" in ret and "<=" in ret:
        if _parse(ret["<="]) < _parse(ret["<"]):
            del ret["<"]
        else:
            del ret["<="]

    if ">=" in ret and ">" in ret:
        if _parse(ret[">="]) > _parse(ret[">"]):
            del ret[">"]
        else:
            del ret[">="]

    if ret == a:
        return a

    if ret == b:
        return b

    ret.pop("special", None)

    return _check_legal(ret)


def interpret(dependency: str) -> dict:
    """
    Interpret a version string.

    :param dependency: Dependency specifier.
    :return: Dictionary::
        name  # name of the dependency
        special  # wildcard version specifier (if used)
        =  # precise version (if used)
        >=  # version range (if used)
        >  # version range (if used)
        <=  # version range (if used)
        <  # version range (if used)
    """

    dep = dependency

    # foo *

    if re.match(r"^([^\*^\s]*)(\s*)(\*)$", dep):
        _, name, _, special, _ = re.split(r"^([^\*^\s]*)(\s*)(\*)$", dep)
        return {"name": name, "special": special}

    # foo =1.0.*

    if re.match(r"^([^=^\s]*)(\s*)([=]*)([^\*]*)(\*)$", dep):

        _, name, _, eq, basename, special, _ = re.split(r"^([^=^\s]*)(\s*)([=]*)([^\*]*)(\*)$", dep)

        if eq != "=":
            raise ValueError(f"Invalid special dependency '{dep}'.")

        if len(basename.split(".")) == 0:
            lower = basename
            upper = f"{int(lower) + 1}"
        else:
            lower = basename.rstrip(".")
            if len(lower.split(".")) == 1:
                upper = f"{int(lower) + 1}"
                if basename[-1] == ".":
                    upper += ".0"
            else:
                base, minor = lower.rsplit(".", 1)
                upper = f"{base}.{int(minor) + 1}.0"

        if len(lower) == 0:
            lower = "0"
        else:
            lower = f"{lower}.0"

        return {"name": name, "special": eq + basename + special, ">=": lower, "<": upper}

    # foo
    # foo =1.0
    # foo >1.0
    # foo >=1.0
    # foo <1.0
    # foo <=1.0
    # foo >1.0, <2.0
    # foo >=1.0, <2.0
    # foo >1.0, <=2.0
    # foo >=1.0, <=2.0

    _, name, _, eq, ver, _ = re.split(r"^([^>^<^=^\s]*)(\s*)([<>=]*)(.*)$", dep)
    eq2 = None
    ver2 = None
    sp = re.split(r"^([^,]*)(,)(\s*)([<>=]*)(.*)$", ver)
    if len(sp) > 1:
        _, ver, _, _, eq2, ver2, _ = sp

    ret = {"name": name}

    if eq == "=" and eq2:
        raise ValueError(f"Cannot have two equalities in '{dep}'")
    if eq in [">=", ">"] and eq2 in [">=", ">"]:
        raise ValueError(f"Illegal bound in '{dep}'")

    for a, b in [(eq, ver), (eq2, ver2)]:
        if not a:
            if b:
                raise ValueError(f"Missing equality in '{dep}'")
            continue
        ret[a] = b

    return _check_legal(ret)


def remove(dependencies: list[str], *args: list[str]) -> list[str]:
    """
    Remove dependencies.

    :param dependencies: List of dependencies.
    :param args: List of dependencies to remove.
    :return: List of dependencies.
    """

    ret = []

    for dep in dependencies:

        dep = re.split("#", dep)[0]

        # foo *

        if re.match(r"^([^\*^\s]*)(\s*)(\*)$", dep):
            name = re.split(r"^([^\*^\s]*)(\s*)(\*)$", dep)[1]

        # foo =1.0.*

        elif re.match(r"^([^=^\s]*)(\s*)([=]*)([^\*]*)(\*)$", dep):
            name = re.split(r"^([^=^\s]*)(\s*)([=]*)([^\*]*)(\*)$", dep)[1]

        # foo
        # foo =1.0
        # foo >1.0
        # foo >=1.0
        # foo <1.0
        # foo <=1.0
        # foo >1.0, <2.0
        # foo >=1.0, <2.0
        # foo >1.0, <=2.0
        # foo >=1.0, <=2.0

        else:
            name = re.split(r"^([^>^<^=^\s]*)(\s*)([<>=]*)(.*)$", dep)[1]

        if name not in args:
            ret.append(dep)

    return ret


def unique(*args) -> list[str]:
    """
    Return a list of unique dependencies.

    :param args: Dependencies to merge.
    :return: List of unique dependencies.
    """

    deps = {}

    for dep in args:

        if "#" in dep:
            dep, comment = dep.split("#", 1)
            warnings.warn(f"Comment '{comment}' ignored.", Warning)

        dep = interpret(dep)

        if dep["name"] in deps:
            deps[dep["name"]] = _merge(deps[dep["name"]], dep)
        else:
            deps[dep["name"]] = dep

    for key in deps:

        if "special" in deps[key]:
            deps[key]["ret"] = f"{key} {deps[key]['special']}"
        else:
            deps[key]["ret"] = (
                key
                + " "
                + ", ".join(
                    [f"{e}{deps[key][e]}" for e in ["=", ">=", ">", "<=", "<"] if e in deps[key]]
                )
            ).strip(" ")

    return [deps[key]["ret"] for key in sorted(deps)]


def _conda_envfile_parse_parser():
    """
    Return parser for :py:func:`conda_envfile_parse`.
    """

    desc = "Parse YAML environnement files."
    parser = argparse.ArgumentParser(description=desc)
    parser.add_argument("--version", action="version", version=version)
    parser.add_argument("files", type=str, nargs="*", help="Input files.")
    return parser


def conda_envfile_parse(args: list[str]):
    """
    Command-line tool to print datasets from a file, see ``--help``.
    :param args: Command-line arguments (should be all strings).
    """

    parser = _conda_envfile_parse_parser()
    args = parser.parse_args(args)

    for filename in args.files:
        env = parse_file(filename)
        env["dependencies"] = unique(*env["dependencies"])
        with open(filename, "w") as file:
            yaml.dump(env, file)


def _conda_envfile_parse_cli():
    conda_envfile_parse(sys.argv[1:])


def _conda_envfile_merge_parser():
    """
    Return parser for :py:func:`conda_envfile_merge`.
    """

    desc = "Merge YAML environnement files."
    parser = argparse.ArgumentParser(description=desc)
    parser.add_argument("-f", "--force", action="store_true", help="Force overwrite output file.")
    parser.add_argument("-o", "--output", type=str, help="Write to output file.")
    parser.add_argument("-a", "--append", type=str, action="append", help="Append dependencies.")
    parser.add_argument("-r", "--remove", type=str, action="append", help="Remove dependencies.")
    parser.add_argument("--version", action="version", version=version)
    parser.add_argument("files", type=str, nargs="*", help="Input files.")
    return parser


def conda_envfile_merge(args: list[str]):
    """
    Command-line tool to print datasets from a file, see ``--help``.
    :param args: Command-line arguments (should be all strings).
    """

    parser = _conda_envfile_merge_parser()
    args = parser.parse_args(args)
    env = parse_file(*args.files)
    env["dependencies"] = unique(*(env["dependencies"] + args.append))

    if args.remove:
        env["dependencies"] = remove(env["dependencies"], *args.remove)

    if not args.output:
        print(yaml.dump(env, default_flow_style=False, default_style="").strip())
        return 0

    dirname = os.path.dirname(args.output)

    if not args.force:
        if os.path.isfile(args.output):
            if not click.confirm(f'Overwrite "{args.output:s}"?'):
                raise OSError("Cancelled")
        elif not os.path.isdir(dirname) and len(dirname) > 0:
            if not click.confirm(f'Create "{os.path.dirname(args.output):s}"?'):
                raise OSError("Cancelled")

    if not os.path.isdir(dirname) and len(dirname) > 0:
        os.makedirs(os.path.dirname(args.output))

    with open(args.output, "w") as file:
        yaml.dump(env, file)


def _conda_envfile_merge_cli():
    conda_envfile_merge(sys.argv[1:])