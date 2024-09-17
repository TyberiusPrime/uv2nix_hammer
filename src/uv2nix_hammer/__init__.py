import sys
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
    get_src,
    drv_to_pkg_and_version,
    log,
    RuleOutput,
    normalize_python_package_name,
    RuleFunctionOutput,
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
name = "app"
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


def write_flake_nix(
    folder,
    uv2nix_repo,
    hammer_overrides_folder,
    python_version,
    nixpkgs_version="24.05",
):
    log.debug(f"Writing flake, python_version={python_version}, nixpkgs={nixpkgs_version}")
    flatpythonver = python_version.replace(".", "")
    (folder / "flake.nix").write_text(f"""
{{
  description = "A basic flake using uv2nix";
  inputs = {{
      nixpkgs.url = "github:nixos/nixpkgs/{nixpkgs_version}";
      uv2nix.url = "{uv2nix_repo}";
      uv2nix.inputs.nixpkgs.follows = "nixpkgs";
      uv2nix_hammer_overrides.url = "{hammer_overrides_folder.absolute()}";
      uv2nix_hammer_overrides.inputs.nixpkgs.follows = "nixpkgs";
  }};
  outputs = {{
    nixpkgs,
    uv2nix,
    uv2nix_hammer_overrides,
    ...
  }}: let
    inherit (nixpkgs) lib;

    workspace = uv2nix.lib.workspace.loadWorkspace {{workspaceRoot = ./.;}};

    pkgs = import nixpkgs {{system="x86_64-linux"; config.allowUnfree = true;}};

    # Manage overlays
    overlay = let
      # Create overlay from workspace.
      overlay' = workspace.mkOverlay {{
        sourcePreference = "wheel";
      }};
      # work around for packaging must-not-be-a-wheel and is best not overwritten
      overlay'' = pyfinal: pyprev: let
        applied = overlay' pyfinal pyprev;
      in
        lib.filterAttrs (n: _: n != "packaging" && n != "tomli" && n != "pyproject-hooks" && n != "build" && n != "wheel" && n!= "pathlib") applied;

       overrides = (uv2nix_hammer_overrides.overrides pkgs);
    in
      lib.composeExtensions overlay'' overrides;

    python = pkgs.python{flatpythonver}.override {{
      self = python;
      packageOverrides = overlay;
    }};
  in {{
    packages.x86_64-linux.default = python.pkgs.app;
    # TODO: A better mkShell withPackages example.
  }};
 }}
""")


def uv_lock(folder):
    subprocess.check_call(
        [
            "uv",
            "lock",
            "--no-cache",
            "--prerelease=allow",
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


def verify_target_on_pypi(pkg, version, cache_folder):
    info = get_pypi_json(pkg, cache_folder)
    if info.get("message") == "Not Found":
        raise ValueError("package not on pypi")
    if version is None:
        releases = {k: v for (k, v) in info["releases"].items() if not is_prerelease(k)}

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
    name = info['info']['name'] # the prefered spelling
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

    #raise ValueError("TODO")  # once I know how to fix this
    cycles = find_cycles_in_uv_lock(project_folder)
    log.debug("Detected infinite recursion")
    if not cycles:
        raise ValueError(
            "infinite recursion encountered, but no cycles found in uv.lock"
        )
    log.debug("Recursion was in uv.lock")
    uv_lock = toml.loads(Path(project_folder / "uv.lock").read_text())
    rules = {}
    seen = set()
    for cycle in cycles:
        if frozenset(cycle) in seen:
            continue
        first_node = cycle[0]  # Should be enough to remove the first edge. Right?
        second_node = cycle[1]  # ignore the others edges in the cycle
        log.debug(f"Fixing by breaking edge {cycle}")
        packages = [(x["name"], x["version"]) for x in uv_lock["package"]]
        matching_pkgs = [x for x in packages if x[0] == first_node]
        first_pkg_version = matching_pkgs[0][1]
        pkg_tuple = first_node, first_pkg_version
        rules[pkg_tuple] = {"RemovePropagatedBuildInputs": second_node}
        # seen.add(frozenset(cycle))
    return rules


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
    stderr = (project_folder / f"run_{attempt_no}.log").read_text()
    if "infinite recursion encountered" in stderr:
        raise InfiniteRecursionError()
    if "pathlib was removed " in stderr:
        raise NeedsExclusion("pathlib was removed in python 3.5")
    if "while evaluating the attribute" in stderr:
        raise ValueError(
            "Generated overwrites were not valid nix code (syntax or semantic)"
        )
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
    failed_drvs = re.findall("error: builder for '(/nix/store/[^']+)' failed", raw)
    return {drv: get_nix_log(drv) for drv in failed_drvs}


def load_existing_rules(overrides_folder, pkg, pkg_version):
    path = overrides_folder / "overrides" / pkg / pkg_version / f"rules.toml"

    if path.exists():
        return toml.load(path)
    else:
        return {}


class NeedsExclusion(Exception):
    pass


def write_combined_rules(path, rules_to_combine, project_folder):
    from .nix_format import nix_format, nix_literal, wrapped_nix_literal

    function_arguments = set()
    src_attrset_parts = {}
    wheel_attrset_parts = {}
    pkg_build_inputs = set()
    further_funcs = []
    requires_nixpkgs_master = False
    dep_constraints = {}
    python_downgrade = None

    for rule_name in rules_to_combine.keys():
        rule = getattr(rules, rule_name)
        rule_output = rule.apply(rules_to_combine[rule_name])
        if isinstance(rule_output, RuleFunctionOutput):
            function_arguments.add("pkgs")  # needed for the pipe
            for arg in ["pkgs", "prev", "final", "helpers"]:
                if f"{arg}." in rule_output.inner:
                    function_arguments.add(arg)
            function_arguments.update(rule_output.args)
            further_funcs.append("old:" + rule_output.inner)
        elif isinstance(rule_output, RuleOutputTriggerExclusion):
            log.info("Triggered exclusion")
            raise NeedsExclusion(rule_output.reason)

        elif isinstance(rule_output, RuleOutput):
            pkg_build_inputs.update(rule_output.build_inputs)
            function_arguments.update(rule_output.arguments)

            for src, dest in (
                (rule_output.src_attrset_parts, src_attrset_parts),
                (rule_output.wheel_attrset_parts, wheel_attrset_parts),
            ):
                for k, v in src.items():
                    if k == "postPatch" or k == "preBuild" or k == "preConfigure":
                        if not k in dest:
                            dest[k] = [nix_literal(f'old.{k} or ""')]
                        dest[k] += [v]
                    elif k == "nativeBuildInputs" or k == "buildInputs":
                        dest[k] = sorted(set(dest.get(k, []) + v))
                    elif not k in dest:
                        dest[k] = v
                    else:
                        raise ValueError(f"Think up a merge strategy for {k}")
            if rule_output.requires_nixpkgs_master:
                log.debug(
                    f"Enabled nixpkgs master because of rule {rule_name} - {path}"
                )
                requires_nixpkgs_master = True
            if rule_output.dep_constraints:
                for k, v in rule_output.dep_constraints.items():
                    if k in dep_constraints and v != dep_constraints[k]:
                        raise ValueError("Dep conflict, think up a merge strategy")
                    dep_constraints[k] = v
            if rule_output.python_downgrade:
                if python_downgrade and python_downgrade != rule_output.python_downgrade:
                    raise ValueError("Conflicting python downgrades")
                python_downgrade = rule_output.python_downgrade
        else:
            raise ValueError(
                f"rule's apply output was not a RuleOutput or RuleFunctionOutput)"
            )

    if pkg_build_inputs:
        function_arguments.add("final")

    pkg_build_inputs = [nix_literal("final." + x) for x in pkg_build_inputs]
    if pkg_build_inputs and src_attrset_parts.get("nativeBuildInputs", []):
        src_attrset_parts["nativeBuildInputs"] = nix_literal(
            "old.nativeBuildInputs or [] ++ "
            + nix_format(src_attrset_parts["nativeBuildInputs"])
            + " ++ "
            + nix_format(pkg_build_inputs)
        )
    elif pkg_build_inputs:
        src_attrset_parts["nativeBuildInputs"] = nix_literal(
            "old.nativeBuildInputs or [] ++ " + nix_format(pkg_build_inputs)
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

    src_body = nix_format(src_attrset_parts)
    wheel_body = nix_format(wheel_attrset_parts)
    funcs = []
    if src_attrset_parts or wheel_attrset_parts:
        funcs.append(
            f"""old: if ((old.format or "sdist") == "wheel") then {wheel_body} else {src_body}"""
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
    if out_body: # no need to write a default.nix if all we did was downgrade pytohn or such
        if function_arguments:
            head = f"{{{", ".join(function_arguments)}, ...}}"
        else:
            head = "{...}"

        path.write_text(head + out_body)
        p = subprocess.Popen(
            ["nix", "fmt", str(path.absolute())],
            cwd=path.parent.parent.parent,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = p.communicate()
        if p.returncode != 0:
            print(stderr)
            raise ValueError(f"nix fmt failed {path.absolute()}")

    if dep_constraints:
        extend_pyproject_toml_with_dep_constraints(
            dep_constraints, project_folder / "pyproject.toml"
        )
    if python_downgrade:
        downgrade_python(python_downgrade, project_folder / "pyproject.toml")

    return requires_nixpkgs_master, python_downgrade


def extend_pyproject_toml_with_dep_constraints(dep_constraints, pyproject_toml_path):
    input = toml.loads(pyproject_toml_path.read_text())
    for k, v in dep_constraints.items():
        input["project"]["dependencies"].append(f"{k}{v}")
    pyproject_toml_path.write_text(toml.dumps(input))
    uv_lock(
        pyproject_toml_path.parent,
    )

def downgrade_python(python_version, pyproject_toml_path):
    log.warn(f"Downgrading to python {python_version}")
    input = toml.loads(pyproject_toml_path.read_text())
    input['project']['requires-python'] = f"~={python_version}"
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



def check_for_wheel_build(drv):
    src = get_src(drv)
    return src.endswith(".whl")


def copy_if_non_value(value):
    try:
        return value.copy()
    except AttributeError:
        return value


def detect_rules(project_folder, overrides_folder, failures):
    """Check which rules we can apply"""
    log.debug(f"Applying rules to {len(failures)} failures")
    any_applied = False
    rules_so_far = {}
    for drv, drv_log in failures.items():
        pkg_tuple = drv_to_pkg_and_version(drv)
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
                    rules_here[rule_name] = opts
                    if opts != old_opts or opts and hasattr(rule, 'always_reapply'):
                        any_applied = True
                        log.info(
                            f"Rule hit! {rule_name} in {pkg_tuple}}}. Now: {opts} - was: {old_opts}"
                        )
                        if hasattr(rule, "extract"):
                            rule.extract(
                                drv,
                                overrides_folder
                                / "overrides"
                                / pkg_tuple[0]
                                / pkg_tuple[1],
                            )

        rules_so_far[pkg_tuple] = rules_here

    return any_applied, rules_so_far


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
        prog="uv2nix_hammer",
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

    return p


def extract_sources(src_folder, failures):
    for drv in failures:
        pkg, version = drv_to_pkg_and_version(drv)
        src = get_src(drv)
        (src_folder / pkg / version).mkdir(exist_ok=True, parents=True)
        if src.endswith(".tar.gz"):
            with tarfile.open(src) as tf:
                tf.extractall(src_folder / pkg / version)
        elif src.endswith(".zip"):
            with zipfile.ZipFile(src) as zf:
                zf.extractall(src_folder / pkg / version)
        elif src.endswith(".whl"):
            pass
        else:
            log.warn(f"Unknown archive type, not unpacked {src}")


def main():
    args = get_parser().parse_args()
    sdist_or_wheel = "wheel" if args.wheel else "sdist"  # the one we put into flake.nix

    overrides_source = "https://github.com/TyberiusPrime/uv2nix_hammer_overrides"
    uv2nix = "github:/adisbladis/uv2nix"

    cache_folder = Path(args.cache_folder or ".uv2nix_hammer_cache")

    if args.rewrite:
        target_pkg = args.target_pkg
        overrides_folder = Path("hammer_build-rewrite")
    else:
        target_pkg = args.target_pkg
        if not target_pkg:
            # raise ValueError("non-rewrite needs target_pkg")
            get_parser().print_help()
            sys.exit(1)
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

    if args.rewrite:
        print(f"rust rewriting rules for {target_pkg}=={target_pkg_version}")
        rules_here = load_existing_rules(
            overrides_folder, target_pkg, target_pkg_version, False
        )
        if not rules_here:
            raise ValueError("No rules")
        # I don't think I'm gonna keep rules_wheel actually
        path = (
            overrides_folder
            / "overrides"
            / target_pkg
            / target_pkg_version
            / "default.nix"
        )
        write_combined_rules(path.with_name("default.nix"), rules_here)

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
        if not (project_folder / "uv.lock").exists():
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
                        "master",
                    )
                try:
                    run_no = attempt_build(project_folder, attempt_no)
                except InfiniteRecursionError:
                    if attempt_no == 0:
                        new_rules = try_to_fix_infinite_recursion(project_folder)
                        requires_nixpkgs_master, python_downgrade = write_rules(True, new_rules, overrides_folder, project_folder)
                        attempt_no += 1
                        continue
                    else:
                        raise
                except ValueError:
                    console.print_exception(show_locals=True)
                    break
                if (project_folder / "result").exists():
                    success = True
                    break
                failures = load_failures(project_folder, run_no)
                any_applied, new_rules = detect_rules(
                    project_folder, overrides_folder, failures
                )
                requires_nixpkgs_master, python_downgrade= write_rules(
                    any_applied, new_rules, overrides_folder, project_folder
                )
                if not any_applied:
                    # we had nothing left to try.
                    break
                attempt_no += 1
        except NeedsExclusion:
            raise  # the helper will read it.

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
