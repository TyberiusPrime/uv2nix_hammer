import sys
import shutil
import time
import datetime
import tarfile
from typing import override
import zipfile

import urllib3
import json
import networkx
import argparse
import toml
import re
from pathlib import Path
import subprocess
from . import rules
from .helpers import (
    drv_to_pkg_and_version,
    extract_source,
    get_src,
    log,
    normalize_python_package_name,
    RuleFunctionOutput,
    RuleOutput,
    RuleOutputCopyFile,
    RuleOutputTriggerExclusion,
)

import rich.traceback
from rich.console import Console

console = Console()
rich.traceback.install(show_locals=True)


def write_pyproject_toml(folder, pkg, pkg_version, sdist_or_wheel, python_version):
    (folder / "pyproject.toml").write_text(
        f"""
[project]
name = "uv2nix-hammer-app"
version = "0.1.0"
description = "Learn to build {pkg}"
requires-python = "~={python_version}"
dependencies = [
    "{pkg}=={pkg_version}",
]
"""
        + (
            ""
            if sdist_or_wheel == "wheel"
            else f"""
[tool.uv]
    no-binary-package = ['{pkg}']
    """
        )
    )


default_nixpkgs_version = "684a8fe32d4b7973974e543eed82942d2521b738"


def write_flake_nix(
    folder,
    uv2nix_repo,
    hammer_overrides_folder,
    python_version,
    nixpkgs_version=default_nixpkgs_version,  # fix for now, there's an issue building the newest uv.
):
    log.debug(
        f"Writing flake, python_version={python_version}, nixpkgs={nixpkgs_version}"
    )
    flatpythonver = python_version.replace(".", "")
    flake_template = (Path(__file__).parent / "flake.template.nix").read_text()
    flake_content = (
        flake_template.replace("{nixpkgs_version}", nixpkgs_version)
        .replace("{uv2nix_repo}", uv2nix_repo)
        .replace("{pyproject_nix_repo}", "github:/nix-community/pyproject.nix")
        .replace("{hammer_overrides_folder}", str(hammer_overrides_folder.absolute()))
        .replace("{flatpythonver}", flatpythonver)
    )

    (folder / "flake.nix").write_text(flake_content)


def uv_lock(folder):
    subprocess.check_call(
        [
            "uv",
            "lock",
            "--no-cache",
            "--prerelease=if-necessary-or-explicit",
        ],
        cwd=folder,
    )


def wrapped_version(x):
    from packaging.version import Version

    try:
        return Version(x)
    except:
        return Version("0.0.0")


def get_pypi_json(pkg, cache_folder, force=False):
    cache_folder.mkdir(exist_ok=True, parents=True)
    fn = cache_folder / f"{pkg}.json"
    if force or not fn.exists() or (fn.stat().st_mtime - time.time()) > 60 * 60 * 24:
        url = f"https://pypi.org/pypi/{pkg}/json"
        resp = urllib3.request("GET", url)
        fn.write_text(resp.data.decode())
    return json.loads(fn.read_text())


def is_prerelease(version_string):
    from packaging.version import Version

    try:
        return Version(version_string).is_prerelease
    except:
        return False


def is_yanked(pkg, info):
    for entry in info:
        if bool(entry.get("yanked", False)):
            return True
    return False


def verify_target_on_pypi(pkg, version, cache_folder):
    info = get_pypi_json(pkg, cache_folder)
    if info.get("message") == "Not Found":
        raise ValueError("package not on pypi")
    if version is None:
        releases = {
            k: v
            for (k, v) in info["releases"].items()
            if not is_prerelease(k) and not is_yanked(k, v)
        }

        # sort with Version aware sort?
        try:
            version = sorted(releases.keys(), reverse=True, key=wrapped_version)[0]
        except IndexError:
            raise ValueError("No non-pre release found")
    else:
        if not version in info["releases"]:
            log.error(f"No release {version} for {pkg} not found on pypi")
            sys.exit(1)

    had_src = False
    for value in info["releases"][version]:
        if value.get("url").endswith(".tar.gz"):
            had_src = True
            break
    name = info["info"]["name"]  # the prefered spelling
    return name, version, had_src


def get_python_release_dates(cache_folder):
    release_dates = {
        "3.13": "2024-10-01",
        "3.12": "2023-10-02",
        "3.11": "2022-10-24",
        "3.10": "2021-10-04",
        "3.9": "2020-10-05",
    }
    release_dates = {
        k: datetime.date.fromisoformat(v) for k, v in release_dates.items()
    }
    return release_dates


def newest_python_at_pkg_release(pkg, version, cache_folder):
    # question 0: what
    info = get_pypi_json(pkg, cache_folder)
    try:
        release = info["releases"][version]
    except KeyError:
        info = get_pypi_json(pkg, cache_folder, True)
        release = info["releases"][version]

    try:
        release_date = datetime.datetime.fromisoformat(release[0]["upload_time"]).date()
    except IndexError:
        raise ValueError("No releases available")
    log.debug(f"Release date for {pkg}=={version} is {release_date}")
    # question 1:
    python_release_dates = [
        (v, k) for (k, v) in get_python_release_dates(cache_folder).items()
    ]
    python_release_dates.sort()
    python_released_before_pkg = [
        (v, k) for (v, k) in python_release_dates if v < release_date
    ]
    if python_released_before_pkg:
        result = python_released_before_pkg[-1][1]
        log.debug(f"Chosen python by 'newest-on-release-date': {result}")
    else:
        result = python_release_dates[0][1]
        log.debug(
            f"Chosen oldest python version we have {result} - pkg is older than that"
        )
    return result


def gitify(folder):
    if not (folder / ".git").exists():
        subprocess.check_call(["git", "init"], cwd=folder, stderr=subprocess.PIPE)
    subprocess.check_call(
        ["git", "add", "flake.nix", "pyproject.toml", "uv.lock"], cwd=folder
    )


def try_to_fix_infinite_recursion(project_folder):
    # situation: we saw' infinite recursion encountered'.
    # let's see if there's a loop in uv.lock
    # and if there is, add a rule to remove it?

    raise ValueError(
        "Pyproject.nix builders should no longer suffer from infinite recursion"
    )  # once I know how to fix this


class InfiniteRecursionError(ValueError):
    pass


def attempt_build(project_folder, attempt_no):
    # attempt_no = 0
    # while (project_folder / f"run_{attempt_no}.log").exists():
    #     attempt_no += 1
    log.info(f"Attempting build, trial no {attempt_no}")
    subprocess.check_call(
        ["nix", "flake", "lock", "--update-input", "uv2nix_hammer_overrides"],
        cwd=project_folder,
        stderr=subprocess.PIPE,
    )
    subprocess.run(
        ["nix", "build", "--keep-going", "--max-jobs", "auto", "--cores", "0"],
        cwd=project_folder,
        stderr=(project_folder / f"run_{attempt_no}.log").open("w"),
    )
    stderr = (
        (project_folder / f"run_{attempt_no}.log")
        .read_bytes()
        .decode("utf-8", errors="replace")
    )
    if "infinite recursion encountered" in stderr:
        raise InfiniteRecursionError()
    if "pathlib was removed " in stderr:
        raise NeedsExclusion("pathlib was removed in python 3.5")
    if "'kaleido' 0.2.1.post1" in stderr:
        raise AddDependency({"kaleido": "==0.2.1"})
    if "attribute 'swig' missing" in stderr:
        raise AddDependency({"swig": ">0"})
    if "attribute 'cysignals' missing" in stderr:
        raise AddDependency({"cysignals": ">0"})
    if "attribute 'requests' missing" in stderr:
        raise AddDependency({"requests": ">0"})
    if "attribute 'torch' missing" in stderr:
        raise AddDependency({"torch": ">0"})
    if "attribute 'versiontools' missing" in stderr:
        raise AddDependency({"versiontools": ">0"})
    if "attribute 'versioneer-518' missing" in stderr:
        raise AddDependency({"versioneer-518": ">0"})
    if "attribute 'certifi' missing" in stderr:
        raise AddDependency({"certifi": ">0"})
    if "attribute 'vcversioner' missing" in stderr:
        raise AddDependency({"vcversioner": ">0"})
    if "attribute 'flake8' missing" in stderr:
        raise AddDependency({"flake8": ">0"})
    if "attribute 'extension-helpers' missing" in stderr:
        raise AddDependency({"extension-helpers": ">0"})
    if "attribute 'isort' missing" in stderr:
        raise AddDependency({"isort": ">0"})
    if "attribute 'pycodestyle' missing" in stderr:
        raise AddDependency({"pycodestyle": ">0"})
    if "attribute 'pytest-benchmark' missing" in stderr:
        raise AddDependency({"pytest-benchmark": ">0"})
    if "attribute 'sphinx' missing" in stderr:
        raise AddDependency({"sphinx": ">0"})
    if "attribute 'pyyaml' missing" in stderr:
        raise AddDependency({"pyyaml": ">0"})
    if "while evaluating the attribute" in stderr:
        raise ValueError(
            "Generated overwrites were not valid nix code (syntax or semantic)"
        )
    if "No compatible wheel, nor sdist found for package" in stderr:
        raise NeedsExclusion("No (compatible) wheel nor sdist found")
    if "OpenSSL 1.1 is reaching its end of life on 2023/09/11" in stderr:
        raise NeedsExclusion("Needs openssl 1.1 which is EOL")

    if dep_constraints := rules.MissingSetParts.match(None, stderr, None, None):
        log.info(f"Adding missing set parts {dep_constraints}")
        raise AddDependency(dep_constraints)

    return attempt_no


def remove_old_logs(project_folder):
    for fn in project_folder.glob("run_*.log"):
        fn.unlink()


def strip_ansi_colors(text):
    return re.sub(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])", "", text)


def get_nix_log(drv):
    return strip_ansi_colors(
        subprocess.check_output(["nix", "log", drv], text=True, stderr=subprocess.PIPE)
    )


def load_failures(project_folder, run_no):
    log_file = project_folder / f"run_{run_no}.log"
    raw = log_file.read_text()
    failed_drvs = re.findall("error: (?:builder for|Cannot build) '(/nix/store/[^']+)'(?:\\.| failed)", raw)
    return {drv: get_nix_log(drv) for drv in failed_drvs if not "test-venv" in drv}


def load_existing_rules(overrides_folder, pkg, pkg_version):
    path = overrides_folder / "overrides" / pkg / pkg_version / f"rules.toml"

    if path.exists():
        return toml.load(path)
    else:
        return {}


class NeedsExclusion(Exception):
    pass


class AddDependency(Exception):
    def __init__(self, deps):
        self.deps = deps
        Exception.__init__(self)


def nix_fmt(path):
    cmd = ["nix", "fmt"]
    if path:
        cmd.append(str(path.absolute()))
    p = subprocess.Popen(
        cmd,
        cwd=path.parent.parent.parent,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = p.communicate()
    if p.returncode != 0:
        print(stderr)
        raise ValueError(f"nix fmt failed {path.absolute()}")


def write_combined_rules(path, rules_to_combine, project_folder, do_format=False):
    from .nix_format import nix_format, nix_literal, wrapped_nix_literal

    assert project_folder is None or isinstance(project_folder, Path)

    function_arguments = set()
    src_attrset_parts = {}
    wheel_attrset_parts = {}
    pkg_build_systems = set()

    further_funcs = []
    requires_nixpkgs_master = False
    dep_constraints = {}
    python_downgrade = None
    additional_pyproject_reqs = set()

    for rule_name in rules_to_combine.keys():
        rule = getattr(rules, rule_name)
        rule_output = rule.apply(rules_to_combine[rule_name])
        if isinstance(rule_output, RuleFunctionOutput):
            function_arguments.add("pkgs")  # needed for the pipe
            for arg in ["pkgs", "prev", "final", "helpers"]:
                if f"{arg}." in rule_output.inner:
                    function_arguments.add(arg)
            function_arguments.update(rule_output.args)
            further_funcs.append("old: " + rule_output.inner)
        elif isinstance(rule_output, RuleOutputTriggerExclusion):
            log.info("Triggered exclusion")
            raise NeedsExclusion(rule_output.reason)
        elif isinstance(rule_output, RuleOutputCopyFile):
            for f in rule_output.files:
                log.debug(f"Copying file {f} to {path.with_name(f.name)}")
                shutil.copy(f, path.with_name(f.name))
        elif isinstance(rule_output, RuleOutput):
            if rule_output.build_systems:
                pkg_build_systems.update(rule_output.build_systems)
            function_arguments.update(rule_output.arguments)

            if rule_output.src_attrset_parts or rule_output.wheel_attrset_parts:
                function_arguments.add("helpers")

            for src, dest in (
                (rule_output.src_attrset_parts, src_attrset_parts),
                (rule_output.wheel_attrset_parts, wheel_attrset_parts),
            ):
                for k, v in src.items():
                    if k == "postPatch" or k == "preBuild" or k == "preConfigure":
                        if not k in dest:
                            dest[k] = [nix_literal(f'old.{k} or ""')]
                        if not v in dest[k]:
                            dest[k] += [v]
                    elif k == "nativeBuildInputs" or k == "buildInputs":
                        dest[k] = sorted(set(dest.get(k, []) + v))
                    elif not k in dest:
                        dest[k] = v
                    elif isinstance(v, dict) and not dest[k] or dest[k] == v:
                        dest[k] = v
                    else:
                        raise ValueError(
                            f"Think up a merge strategy for {k} {repr(dest[k])} vs {repr(v)}"
                        )
            if rule_output.requires_nixpkgs_master:
                # log.debug(
                #     f"Enabled nixpkgs master because of rule {rule_name} - {path}"
                # )
                requires_nixpkgs_master = True
            if rule_output.dep_constraints:
                for k, v in rule_output.dep_constraints.items():
                    if k in dep_constraints and v != dep_constraints[k]:
                        raise ValueError("Dep conflict, think up a merge strategy")
                    dep_constraints[k] = v
            if rule_output.python_downgrade:
                if (
                    python_downgrade
                    and python_downgrade != rule_output.python_downgrade
                ):
                    raise ValueError("Conflicting python downgrades")
                python_downgrade = rule_output.python_downgrade
        else:
            raise ValueError(
                f"rule's apply output was not a RuleOutput or RuleFunctionOutput)"
            )

    if pkg_build_systems:
        function_arguments.add("final")
        function_arguments.add("helpers")
        function_arguments.add("resolveBuildSystem")

    pkg_build_systems = {x: [] for x in pkg_build_systems}
    if pkg_build_systems and src_attrset_parts.get("nativeBuildInputs", []):
        src_attrset_parts["nativeBuildInputs"] = nix_literal(
            "old.nativeBuildInputs or [] ++ "
            + nix_format(src_attrset_parts["nativeBuildInputs"])
            + " ++ "
            + "( resolveBuildSystem "
            + nix_format(pkg_build_systems)
            + ")"
        )
    elif pkg_build_systems:
        src_attrset_parts["nativeBuildInputs"] = nix_literal(
            "old.nativeBuildInputs or [] ++ "
            + "( resolveBuildSystem "
            + nix_format(pkg_build_systems)
            + ")"
        )
    elif src_attrset_parts.get("nativeBuildInputs", []):
        src_attrset_parts["nativeBuildInputs"] = nix_literal(
            "old.nativeBuildInputs or [] ++ "
            + nix_format(src_attrset_parts["nativeBuildInputs"])
        )

    if "buildInputs" in src_attrset_parts:
        src_attrset_parts["buildInputs"] = nix_literal(
            "old.buildInputs or [] ++ " + nix_format(src_attrset_parts["buildInputs"])
        )
    if "buildInputs" in wheel_attrset_parts:
        wheel_attrset_parts["buildInputs"] = nix_literal(
            "old.buildInputs or [] ++ " + nix_format(wheel_attrset_parts["buildInputs"])
        )

    if "postPatch" in src_attrset_parts:
        src_attrset_parts["postPatch"] = nix_literal(
            "+".join(
                ("(" + nix_format(x) + ")" for x in src_attrset_parts["postPatch"])
            )
        )

    if "env" in src_attrset_parts and not src_attrset_parts["env"]:
        del src_attrset_parts["env"]

    # log.info(f"pkg_build_systems {pkg_build_systems}")
    src_body = nix_format(src_attrset_parts)
    wheel_body = nix_format(wheel_attrset_parts)
    funcs = []
    if src_attrset_parts or wheel_attrset_parts:
        if src_body == wheel_body:
            funcs.append(f"""old: {src_body}""")

        else:
            funcs.append(
                f"""old: if (helpers.isWheel old) then {wheel_body} else {src_body}"""
            )
    funcs.extend(further_funcs)
    if len(funcs) == 1:
        out_body = f"""
        : {funcs[0]}
        """
    elif len(funcs) > 0:
        str_funcs = nix_format(
            [
                wrapped_nix_literal(f.replace("old:", "old: old // (") + ")")
                for f in funcs
            ]
        )
        function_arguments.add("pkgs")

        out_body = f"""
        :
            old:
            let funcs = {str_funcs};
            in
            pkgs.lib.trivial.pipe old funcs
    """
    else:
        out_body = False
    if (
        out_body
    ):  # no need to write a default.nix if all we did was downgrade pytohn or such
        if function_arguments:
            head = f"{{{", ".join(sorted(function_arguments))}, ...}}"
        else:
            head = "{...}"

        path.write_text(head + out_body)
        if do_format:
            nix_format(path)
    else:
        print("no body")

    if dep_constraints and project_folder is not None:
        extend_pyproject_toml_with_dep_constraints(
            dep_constraints, project_folder / "pyproject.toml"
        )
    if python_downgrade and project_folder is not None:
        downgrade_python(python_downgrade, project_folder / "pyproject.toml")
    if additional_pyproject_reqs and project_folder is not None:
        extend_pyproject_toml_with_dep_constraints(
            additional_pyproject_reqs, project_folder / "pyproject.toml"
        )

    return requires_nixpkgs_master, python_downgrade


def downgrade_python(python_version, pyproject_toml_path):
    log.warn(f"Downgrading to python {python_version}")
    input = toml.loads(pyproject_toml_path.read_text())
    input["project"]["requires-python"] = f"~={python_version}"
    pyproject_toml_path.write_text(toml.dumps(input))
    uv_lock(
        pyproject_toml_path.parent,
    )
    # flake_input = pyproject_toml_path.with_name('flake.nix').read_text()
    # flat_py_version = python_version.replace(".", "")
    # flake_output = flake_input.replace('pkgs.python312',
    #                                  f'pkgs.python{flat_py_version}')
    # assert flake_input != flake_output
    # pyproject_toml_path.with_name('flake.nix').write_text(flake_output)


def extend_pyproject_toml_with_dep_constraints(dep_constraints, pyproject_toml_path):
    input = toml.loads(pyproject_toml_path.read_text())
    for k, v in dep_constraints.items():
        input["project"]["dependencies"].append(f"{k}{v}")
    pyproject_toml_path.write_text(toml.dumps(input))
    uv_lock(
        pyproject_toml_path.parent,
    )


def check_for_wheel_build(drv):
    src = get_src(drv)
    return src.endswith(".whl")


def copy_if_non_value(value):
    try:
        return value.copy()
    except AttributeError:
        return value


def detect_rules(project_folder, overrides_folder, failures, current_python):
    """Check which rules we can apply"""
    log.debug(f"Applying rules to {len(failures)} failures")
    any_applied = False
    rules_so_far = {}
    for drv, drv_log in failures.items():
        pkg_tuple = drv_to_pkg_and_version(drv)
        if not pkg_tuple[0]:
            raise ValueError(f"extracted empty pkg name from {drv}")
        if pkg_tuple[0] in (
            "bootstrap-packaging",
            "bootstrap-tomli",
            "bootstrap-build",
        ):
            log.warn(
                f"Skipping detect_rules for {pkg_tuple[0]} - we're not going to override that, it needs to bootstrap from nixpkgs"
            )
            continue
        # is_wheel = check_for_wheel_build(drv)
        rules_here = load_existing_rules(overrides_folder, *pkg_tuple)
        for rule_name in dir(rules):
            rule = getattr(rules, rule_name)
            if (
                isinstance(rule, type)
                and issubclass(rule, rules.Rule)
                and rule is not rules.Rule
            ):
                # log.debug(f"checking rule {rule_name} in {pkg_tuple}")
                old_opts = rules_here.get(rule_name)
                if opts := rule.match(
                    drv, drv_log, copy_if_non_value(old_opts), rules_here.copy()
                ):
                    log.debug(
                        f"Got back for rule {rule} -value: {opts} - old was {old_opts}. Current_python {current_python}"
                    )

                    rules_here[rule_name] = opts
                    if (
                        (opts != old_opts)
                        or (opts and hasattr(rule, "always_reapply"))
                        or (
                            isinstance(rule, type)
                            and issubclass(rule, rules.DowngradePython)
                            and (opts != current_python)
                        )
                    ):
                        any_applied = True
                        log.info(
                            f"Rule hit! {rule_name} in {pkg_tuple}}}. Now: {opts} - was: {old_opts}"
                        )
                        if hasattr(rule, "extract"):
                            log.warning(f"Had extract {rule}")
                            rules_here[rule_name] = (
                                rules_here[rule_name],
                                rule.extract(
                                    drv,
                                    overrides_folder
                                    / "overrides"
                                    / pkg_tuple[0]
                                    / pkg_tuple[1],
                                ),
                            )

        rules_so_far[pkg_tuple] = rules_here

    return any_applied, rules_so_far


def collect_and_commit(overrides_folder):
    collect_overwrites(overrides_folder)
    # we have to commit every time for nix > 2.23 or such
    # see https://github.com/NixOS/nix/issues/11181
    subprocess.run(
        [
            "git",
            "add",
            ".",
        ],
        cwd=overrides_folder,
    )
    subprocess.run(
        [
            "git",
            "commit",
            "-m",
            f"autogenerated overwrites (commit to be squashed)",
        ],
        cwd=overrides_folder,
    )


def write_rules(any_applied, rules_so_far, overrides_folder, project_folder):
    requires_nixpkgs_master = False
    python_downgrade = None
    if any_applied:  # todo move up...
        for ((pkg, version)), rules_here in rules_so_far.items():
            if not rules_here:
                continue
            path = overrides_folder / "overrides" / pkg / version / f"rules.toml"
            path.parent.mkdir(exist_ok=True, parents=True)
            rules_here = {k: rules_here[k] for k in sorted(rules_here.keys())}
            toml.dump(rules_here, path.open("w"))
            _requires_nixpkgs_master, python_downgrade = write_combined_rules(
                path.with_name("default.nix"), rules_here, project_folder
            )
            requires_nixpkgs_master |= _requires_nixpkgs_master

        collect_and_commit(overrides_folder)
    return requires_nixpkgs_master, python_downgrade


def collect_overwrites(overrides_folder):
    p = subprocess.Popen(
        ["python", "dev/collect.py"],
        cwd=overrides_folder,
        stderr=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    stdout, stderr = p.communicate()
    if p.returncode != 0:
        log.error(stderr)
        raise ValueError(f"Failed to collect overrides with dev/collect.py: ")
    subprocess.check_call(["git", "add", "."], cwd=overrides_folder)


def clear_existing_overrides(
    overrides_folder, target_pkg, target_pkg_version, sdist_or_wheel
):
    """Note that sdist_or_wheel is for this package and derived from pypi
    if --sdist is not set. Package might not have a wheel dist.
    """
    pass
    # is_wheel = sdist_or_wheel == "wheel"
    # default_nix = (
    #     overrides_folder / "overrides" / trget_pkg / target_pkg_version / "default.nix"
    # )
    # default_nix.parent.mkdir(exist_ok=True, parents=True)
    # collect = False
    # if default_nix.exists():
    #     default_nix.unlink()
    #     collect = True
    # rules_toml = (
    #     overrides_folder
    #     / "overrides"
    #     / target_pkg
    #     / target_pkg_version
    #     / f"rules_{'wheel' if is_wheel else 'src'}.toml"
    # )
    # if rules_toml.exists():
    #     rules_toml.unlink()

    # if collect:
    #     collect_overwrites(overrides_folder)


def get_parser():
    p = argparse.ArgumentParser(
        prog="uv2nix-hammer",
        description="Autogenerate overrides for uv2nix usage",
        epilog="Because the existance of nails implies the existance of at least one hammer",
    )
    p.add_argument(
        "target_pkg",
        type=str,
        help="The package to build (from pypi)",
        nargs="?",
    )
    p.add_argument(
        "target_pkg_version",
        type=str,
        help='Version of the package to build. Optional, defaults to "newest"',
        nargs="?",
    )
    p.add_argument(
        "-r",
        "--rewrite",
        action="store_true",
        help="If set, only rewrite default.nix from rules.",
    )
    p.add_argument(
        "-w",
        "--wheel",
        action="store_true",
        help="Whether to build the top level pkg from sdist or wheel. Defaults to sdist",
    )
    p.add_argument(
        "-o",
        "--overrides-folder",
        action="store",
        help="Use a different folder for the cloned overrides (allowing for multiple builds)",
    )
    p.add_argument(
        "-m",
        "--manual-overrides-source-folder",
        action="store",
        help="Rsync manual overrides from this folder before running (not top level, manual_overrides!)",
    )
    p.add_argument(
        "-p",
        "--python-version",
        action="store",
        help="python to use. Default to 'newest at time of package release'",
    )
    p.add_argument(
        "-c",
        "--cache-folder",
        action="store",
        help="Cache folder to store pypi lookups etc. Defaults to .uv2nix_hammer_cache",
    )

    p.add_argument(
        "-s",
        "--override_source",
        action="store",
        help="Url for the flake repository with the uv2nix_hammer_overrides",
        default="https://github.com/TyberiusPrime/uv2nix_hammer_overrides",
    )

    return p


def extract_sources(src_folder, failures):
    for drv in failures:
        pkg, version = drv_to_pkg_and_version(drv)
        (src_folder / pkg / version).mkdir(exist_ok=True, parents=True)
        try:
            src = get_src(drv)
            extract_source(src, (src_folder / pkg / version))
        except KeyError:
            log.error(f"Failed to extract source for {pkg}=={version}")


def apply_all_manual_overrides(overrides_folder):
    """We need to make sure all manual overrides are
    in place - there are packages that don't fail without the overrides
    but still need files deleted, and this is the way to get that done"""
    changed = False
    for pkg_dir in sorted(
        (x for x in Path(overrides_folder / "manual_overrides").iterdir() if x.is_dir())
    ):
        for version_dir in sorted((x for x in pkg_dir.iterdir() if x.is_dir())):
            if (version_dir / "default.nix").exists():
                old = (version_dir / "default.nix").read_text()
            else:
                old = ""
            pkg = pkg_dir.name
            version = version_dir.name
            rules_so_far = load_existing_rules(overrides_folder, pkg, version)
            rules_so_far["ManualOverrides"] = f"__file__:{pkg}/{version}/default.nix"
            target_path = overrides_folder / "overrides" / pkg / version / "default.nix"
            # log.debug(
            #     f"Preloading manual overrides for {pkg}=={version} into {target_path}"
            # )
            target_path.parent.mkdir(exist_ok=True, parents=True)
            write_combined_rules(target_path, rules_so_far, None)
            toml.dump(rules_so_far, open(target_path.with_name("rules.toml"), "w"))
            new = (target_path).read_text()
            changed = changed | (old != new)
    if changed:
        log.debug("Recollecting overrides")
        collect_and_commit(overrides_folder)


def main():
    args = get_parser().parse_args()
    sdist_or_wheel = "wheel" if args.wheel else "sdist"  # the one we put into flake.nix

    overrides_source = args.override_source
    uv2nix = "github:/adisbladis/uv2nix"

    cache_folder = Path(args.cache_folder or ".uv2nix_hammer_cache")

    if args.rewrite:
        target_pkg = args.target_pkg
        target_pkg_version = args.target_pkg_version
        overrides_folder = Path("hammer_build-rewrite")
    else:
        target_pkg = args.target_pkg
        if not target_pkg:
            # raise ValueError("non-rewrite needs target_pkg")
            get_parser().print_help()
            sys.exit(1)
        if target_pkg.startswith("hammer_build"):  # so I can autocomplete on the shell
            if target_pkg.endswith("/"):
                target_pkg = target_pkg[:-1]
            _, _, target_pkg, target_pkg_version = target_pkg.split("_")
        else:
            target_pkg_version = args.target_pkg_version
        target_pkg, target_pkg_version, had_src = verify_target_on_pypi(
            target_pkg, target_pkg_version, cache_folder
        )
        target_folder = Path(
            f"hammer_build_{normalize_python_package_name(target_pkg)}_{target_pkg_version}"
        )
        target_folder.mkdir(exist_ok=True)
        project_folder = target_folder / "build"
        src_folder = target_folder / "src"
        if args.overrides_folder:
            overrides_folder = Path(args.overrides_folder)
            log.info(f"Using overrides_folder {overrides_folder}")
        else:
            overrides_folder = target_folder / "overrides"
        python_version = args.python_version or newest_python_at_pkg_release(
            target_pkg, target_pkg_version, cache_folder
        )

    # clone th overrides repo
    if not overrides_folder.exists():
        overrides_folder.mkdir(exist_ok=True)
        log.debug("git cloning hammer_overrides")
        subprocess.run(
            ["git", "clone", overrides_source, str(overrides_folder)],
            stderr=subprocess.PIPE,
        )
        subprocess.run(
            ["git", "switch", "-c", f"{target_pkg}-{target_pkg_version}"],
            cwd=overrides_folder,
        )
    rules.manual_rule_path = overrides_folder / "manual_overrides"
    if args.manual_overrides_source_folder:
        if (Path(args.manual_overrides_source_folder) / "manual_overrides").exists():
            p = Path(args.manual_overrides_source_folder) / "manual_overrides"
        else:
            p = Path(args.manual_overrides_source_folder)
        if not p.exists():
            raise ValueError(f"manual_overrides_source_folder {p} does not exist")
        assert p.name == "manual_overrides"
        print(
            [
                "rsync",
                str(p),
                overrides_folder,
                "-r",
                "--info=progress2",
            ]
        )
        subprocess.check_call(
            [
                "rsync",
                str(p),
                overrides_folder,
                "-r",
                "--info=progress2",
            ]
        )
        apply_all_manual_overrides(overrides_folder)

    if args.rewrite:
        log.info(f"rust rewriting rules for {target_pkg}=={target_pkg_version}")
        rules_here = load_existing_rules(
            overrides_folder, target_pkg, target_pkg_version
        )
        if not rules_here:
            raise ValueError("No rules")
        path = (
            overrides_folder
            / "overrides"
            / target_pkg
            / target_pkg_version
            / "default.nix"
        )
        write_combined_rules(path.with_name("default.nix"), rules_here, None)

    else:
        project_folder.mkdir(exist_ok=True)
        # todo: dependency tracking?
        write_pyproject_toml(
            project_folder,
            target_pkg,
            target_pkg_version,
            sdist_or_wheel,
            python_version,
        )
        # if not (project_folder / "uv.lock").exists():
        uv_lock(project_folder)
        # if not (project_folder / "flake.nix").exists():
        write_flake_nix(project_folder, uv2nix, overrides_folder, python_version)
        gitify(project_folder)

        remove_old_logs(project_folder)

        if sdist_or_wheel == "wheel":
            clear = "wheel"  # we'll just assume you had a wheel...
        else:
            if had_src:
                clear = "sdist"
            else:
                clear = "wheel"

        clear_existing_overrides(
            overrides_folder, target_pkg, target_pkg_version, clear
        )
        if (project_folder / "result").exists():
            (project_folder / "result").unlink()

        max_trials = 10
        success = False
        failures = []
        attempt_no = 0
        requires_nixpkgs_master = None
        python_downgrade = None
        try:
            while attempt_no < max_trials:
                if requires_nixpkgs_master or python_downgrade:
                    write_flake_nix(
                        project_folder,
                        uv2nix,
                        overrides_folder,
                        python_version if not python_downgrade else python_downgrade,
                        default_nixpkgs_version,
                    )
                try:
                    run_no = attempt_build(project_folder, attempt_no)
                except InfiniteRecursionError:
                    if attempt_no == 0:
                        new_rules = try_to_fix_infinite_recursion(project_folder)
                        requires_nixpkgs_master, python_downgrade = write_rules(
                            True, new_rules, overrides_folder, project_folder
                        )
                        attempt_no += 1
                        continue
                    else:
                        raise
                except AddDependency as e:
                    log.warn(f"Adding dep from AddDependency {e}")
                    extend_pyproject_toml_with_dep_constraints(
                        e.deps, project_folder / "pyproject.toml"
                    )
                    attempt_no += 1
                    continue
                except ValueError:
                    console.print_exception(show_locals=True)
                    break
                if (project_folder / "result").exists():
                    success = True
                    break
                failures = load_failures(project_folder, run_no)
                any_applied, new_rules = detect_rules(
                    project_folder,
                    overrides_folder,
                    failures,
                    python_version if not python_downgrade else python_downgrade,
                )
                requires_nixpkgs_master, python_downgrade = write_rules(
                    any_applied, new_rules, overrides_folder, project_folder
                )
                if not any_applied:
                    # we had nothing left to try.
                    break
                attempt_no += 1
        except NeedsExclusion:
            raise  # the helper will read it.
        except Exception as e:
            success = False
            log.error("Exception in attempt_to_build loop")
            log.error(f"{e}")
            console.print_exception(show_locals=True)

        if success:
            subprocess.run(
                [
                    "git",
                    "add",
                    ".",
                ],
                cwd=overrides_folder,
            )
            subprocess.run(
                [
                    "git",
                    "commit",
                    "-m",
                    f"autogenerated overwrites for {target_pkg}=={target_pkg_version}",
                ],
                cwd=overrides_folder,
            )
            log.info(f"We had success building the packages")
            head_branches = subprocess.check_output(
                ["git", "branch", "--contains", "HEAD"], text=True, cwd=overrides_folder
            )
            if "main" in head_branches:
                log.info("No changes were necessary")
            else:
                # squash the commits down to a single one
                # non interactive!
                head_rev = (
                    subprocess.check_output(
                        ["git", "merge-base", "main", "HEAD"],
                        cwd=overrides_folder,
                    )
                    .decode()
                    .strip()
                )
                subprocess.check_call(
                    ["git", "reset", head_rev, "--soft"], cwd=overrides_folder
                )
                # now commit
                subprocess.check_call(
                    [
                        "git",
                        "commit",
                        "-m",
                        f"autogenerated overwrites for {target_pkg}=={target_pkg_version}",
                    ],
                    cwd=overrides_folder,
                )

                log.info(
                    f"Check out the (commited) overrides in {overrides_folder} using `git diff HEAD~1 HEAD`"
                )
        else:
            extract_sources(src_folder, failures)
            log.error(
                f"We failed to achive success in build. Read the error logs in {project_folder} and try to extend the rule set?"
            )
            log.info(
                f"The sources of failing packages has been extracted to {src_folder}"
            )
            if max_trials == 0:
                log.info("We gave up because the maximum number of trials was reached")
            else:
                log.info("we gave up because we had no further rules")
            sys.exit(1)


def find_cycles_in_uv_lock(path):
    input = toml.loads((Path(path) / "uv.lock").read_text())
    g = networkx.DiGraph()
    for package in input["package"]:
        for dep in package.get("dependencies", []):
            g.add_edge(dep["name"], package["name"])
    return networkx.find_cycle(g)


def main_find_infinite_recursion():
    """Attempt to find cycles in the dependency graphin uv.lock"""
    import networkx

    print(find_cycles_in_uv_lock("."))


def get_parser_rewrite_all():
    p = argparse.ArgumentParser(
        prog="uv2nix-hammer-rewrite-all",
        description="Rewrite all overrides from their rules.",
        epilog="",
    )


def main_rewrite_all():
    parser = get_parser_rewrite_all()
    args = get_parser().parse_args()
    target_folder = Path(".")
    overrides_folder = target_folder / "overrides"
    if not overrides_folder.exists():
        raise ValueError("run from uv2nix_hammer_overrides checkout folder")

    rules.manual_rule_path = target_folder / "manual_overrides"
    for pkg_folder in (x for x in overrides_folder.iterdir() if x.is_dir()):
        pkg = pkg_folder.name
        if pkg == "jaeger-client":
            continue
        for version_folder in (x for x in pkg_folder.iterdir() if x.is_dir()):
            version = version_folder.name
            try:
                rules_here = load_existing_rules(target_folder, pkg, version)
            except toml.TomlDecodeError:
                print(rules_here)
            write_combined_rules(version_folder / "default.nix", rules_here, None)
