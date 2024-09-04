import sys
import tarfile
from typing import override
import zipfile
import urllib3
import json
import argparse
import toml
import re
from pathlib import Path
import subprocess
from . import rules
from .helpers import get_src, drv_to_pkg_and_version, log, RuleOutput

import rich.traceback

rich.traceback.install(show_locals=True)


def write_pyproject_toml(folder, pkg, pkg_version):
    (folder / "pyproject.toml").write_text(
        f"""
[project]
name = "app"
version = "0.1.0"
description = "Learn to build {pkg}"
requires-python = ">=3.11"
dependencies = [
    "{pkg}=={pkg_version}",
]
"""
    )


def write_flake_nix(
    folder, uv2nix_repo, hammer_overrides_folder, wheel_or_sdist="wheel"
):
    (folder / "flake.nix").write_text(f"""
{{
  description = "A basic flake using uv2nix";
  inputs = {{
      nixpkgs.url = "github:nixos/nixpkgs/24.05";
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

    # Manage overlays
    overlay = let
      # Create overlay from workspace.
      overlay' = workspace.mkOverlay {{
        sourcePreference = "{wheel_or_sdist}";
      }};
       overrides = (uv2nix_hammer_overrides.overrides);
    in
      lib.composeExtensions overlay' overrides;

    pkgs = nixpkgs.legacyPackages.x86_64-linux;
    python = pkgs.python3.override {{
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
    subprocess.check_call(["uv", "lock", "--no-cache"], cwd=folder)


def verify_target_on_pypi(pkg, version):
    from packaging.version import Version

    url = f"https://pypi.org/pypi/{pkg}/json"
    resp = urllib3.request("GET", url)
    json = resp.json()
    if json.get("message") == "Not Found":
        raise ValueError("package not on pypi")
    if version is None:
        releases = json["releases"]
        # sort with Version aware sort?
        version = sorted(releases.keys(), reverse=True, key=Version)[0]
    else:
        if not version in json["releases"]:
            log.error(f"No release {version} for {pkg} not found on pypi")
            sys.exit(1)

    had_src = False
    for value in json["releases"][version]:
        if value.get("url").endswith(".tar.gz"):
            had_src = True
            break
    return version, had_src


def gitify(folder):
    if not (folder / ".git").exists():
        subprocess.check_call(["git", "init"], cwd=folder, stderr=subprocess.PIPE)
    subprocess.check_call(
        ["git", "add", "flake.nix", "pyproject.toml", "uv.lock"], cwd=folder
    )


def attempt_build(project_folder):
    attempt_no = 0
    while (project_folder / f"run_{attempt_no}.log").exists():
        attempt_no += 1
    subprocess.check_call(
        ["nix", "flake", "lock", "--update-input", "uv2nix_hammer_overrides"],
        cwd=project_folder,
        stderr=subprocess.PIPE,
    )
    subprocess.run(
        ["nix", "build", "--keep-going"],
        cwd=project_folder,
        stderr=(project_folder / f"run_{attempt_no}.log").open("w"),
    )
    stderr = (project_folder / f"run_{attempt_no}.log").read_text()
    if "while evaluating the attribute" in stderr:
        raise ValueError(
            "Generated overwrites were not valid nix code (syntax or semantic)"
        )
    return attempt_no


def remove_old_logs(project_folder):
    for fn in project_folder.glob("run_*.log"):
        fn.unlink()


def get_nix_log(drv):
    return subprocess.check_output(
        ["nix", "log", drv], text=True, stderr=subprocess.PIPE
    )


def load_failures(project_folder, run_no):
    log_file = project_folder / f"run_{run_no}.log"
    raw = log_file.read_text()
    failed_drvs = re.findall("error: builder for '(/nix/store/[^']+)' failed", raw)
    return {drv: get_nix_log(drv) for drv in failed_drvs}


def load_existing_rules(overrides_folder, pkg, pkg_version, is_wheel):
    path = (
        overrides_folder
        / "overrides"
        / pkg
        / pkg_version
        / f"rules_{'wheel' if is_wheel else 'src'}.toml"
    )

    print(path)
    if path.exists():
        return toml.load(path)
    else:
        return {}


def write_combined_rules(path, rules_to_combine):
    from .nix_format import nix_format, nix_literal
    print(rules_to_combine)

    function_arguments = set()
    src_attrset_parts = {}
    wheel_attrset_parts = {}
    pkg_build_inputs = set()

    for rule_name in rules_to_combine.keys():
        rule = getattr(rules, rule_name)
        rule_output = rule.apply(rules_to_combine[rule_name])
        if not isinstance(rule_output, RuleOutput):
            raise ValueError(f"rule's apply output was not a RuleOutput)")
        pkg_build_inputs.update(rule_output.build_inputs)
        function_arguments.update(rule_output.arguments)

        for src, dest in (
            (rule_output.src_attrset_parts, src_attrset_parts),
            (rule_output.wheel_attrset_parts, wheel_attrset_parts),
        ):
            for k, v in src.items():
                if not k in dest:
                    dest[k] = v
                elif k == "patchPhase":
                    dest[k] += " + " + v
                else:
                    raise ValueError(f"Think up a merge strategy for {k}")

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

    src_body = nix_format(src_attrset_parts)
    wheel_body = nix_format(wheel_attrset_parts)
    path.write_text(f"""
        {{{", ".join(function_arguments)}, ...}}: old: if ((old.format or "sdist") == "wheel") then {wheel_body} else {src_body}
    """)
    subprocess.check_call(["nix", "fmt", str(path.absolute())], cwd=path.parent)


def check_for_wheel_build(drv):
    src = get_src(drv)
    return src.endswith(".whl")


def copy_if_non_value(value):
    try:
        return value.copy()
    except AttributeError:
        return value


def apply_rules(project_folder, overrides_folder, failures):
    log.debug(f"Applying rules to {len(failures)} failures")
    any_applied = False
    rules_so_far = {}
    for drv, drv_log in failures.items():
        pkg_tuple = drv_to_pkg_and_version(drv)
        is_wheel = check_for_wheel_build(drv)
        rules_here = load_existing_rules(overrides_folder, *pkg_tuple, is_wheel)
        for rule_name in dir(rules):
            rule = getattr(rules, rule_name)
            # todo: check if it's a helpers.Rule
            if isinstance(rule, type) and issubclass(rule, rules.Rule) and rule is not rules.Rule:
                log.debug(f"checking rule {rule_name} in {pkg_tuple}")
                old_opts = rules_here.get(rule_name)
                if opts := rule.match(drv, drv_log, copy_if_non_value(old_opts)):
                    log.debug(f"\t Hit! {opts}")
                    rules_here[rule_name] = opts
                    any_applied = opts != old_opts
        rules_so_far[pkg_tuple, is_wheel] = rules_here

    return any_applied, rules_so_far


def write_rules(any_applied, rules_so_far, overrides_folder):
    if any_applied: # todo move up...
        for ((pkg, version), is_wheel), rules_here in rules_so_far.items():
            path = (
                overrides_folder
                / "overrides"
                / pkg
                / version
                / f"rules_{'wheel' if is_wheel else 'src'}.toml"
            )
            path.parent.mkdir(exist_ok=True, parents=True)
            toml.dump(rules_here, path.open("w"))
            write_combined_rules(path.with_name("default.nix"), rules_here)
        collect_overwrites(overrides_folder)
        return any_applied


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
    is_wheel = sdist_or_wheel == "wheel"
    default_nix = (
        overrides_folder / "overrides" / target_pkg / target_pkg_version / "default.nix"
    )
    default_nix.parent.mkdir(exist_ok=True, parents=True)
    collect = False
    if default_nix.exists():
        default_nix.unlink()
        collect = True
    rules_toml = (
        overrides_folder
        / "overrides"
        / target_pkg
        / target_pkg_version
        / f"rules_{'wheel' if is_wheel else 'src'}.toml"
    )
    if rules_toml.exists():
        rules_toml.unlink()

    if collect:
        collect_overwrites(overrides_folder)


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
    )
    p.add_argument(
        "target_pkg_version",
        type=str,
        help='Version of the package to build. Optional, defaults to "newest"',
        nargs="?",
    )
    p.add_argument('-r','--rewrite', action="store_true", help="If set, only rewrite default.nix from rules.")
    p.add_argument(
        "-s",
        "--sdist",
        action="store_true",
        help="Whether to build from sdist or wheel. Defaults to wheel",
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
        else:
            log.warn(f"Unknown archive type, not unpacked {src}")


def main():
    args = get_parser().parse_args()
    target_pkg = args.target_pkg
    target_pkg_version = args.target_pkg_version
    target_pkg_version, had_src = verify_target_on_pypi(target_pkg, target_pkg_version)
    sdist_or_wheel = "sdist" if args.sdist else "wheel"  # the one we put into flake.nix

    overrides_source = "https://github.com/TyberiusPrime/uv2nix_hammer_overrides"
    uv2nix = "github:/adisbladis/uv2nix"

    if args.rewrite:
        overrides_folder = Path("hammer_build-rewrite")
    else:
        target_folder = Path(f"hammer_build_{target_pkg}_{target_pkg_version}")
        target_folder.mkdir(exist_ok=True)
        project_folder = target_folder / "build"
        src_folder = target_folder / "src"
        overrides_folder = target_folder / "overrides"

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

    if args.rewrite:
        print(f"rust rewriting rules for {target_pkg}=={target_pkg_version}") 
        rules_here = load_existing_rules(overrides_folder, target_pkg, target_pkg_version, False)
        if not rules_here:
            raise ValueError("No rules")
        # I don't think I'm gonna keep rules_wheel actually
        path = overrides_folder / "overrides" / target_pkg / target_pkg_version / "default.nix"
        write_combined_rules(path.with_name("default.nix"), rules_here)


    else:

        project_folder.mkdir(exist_ok=True)
        # todo: dependency tracking?
        if not (project_folder / "pyproject.toml").exists():
            write_pyproject_toml(project_folder, target_pkg, target_pkg_version)
        if not (project_folder / "uv.lock").exists():
            uv_lock(project_folder)
        # if not (project_folder / "flake.nix").exists():
        write_flake_nix(project_folder, uv2nix, overrides_folder, sdist_or_wheel)
        gitify(project_folder)

        remove_old_logs(project_folder)

        if sdist_or_wheel == "wheel":
            clear = "wheel"  # we'll just assume you had a wheel...
        else:
            if had_src:
                clear = "sdist"
            else:
                clear = "wheel"

        clear_existing_overrides(overrides_folder, target_pkg, target_pkg_version, clear)
        if (project_folder / "result").exists():
            (project_folder / "result").unlink()

        max_trials = 10
        success = False
        while max_trials > 0:
            run_no = attempt_build(project_folder)
            if (project_folder / "result").exists():
                success = True
                break
            failures = load_failures(project_folder, run_no)
            any_applied, new_rules = apply_rules(project_folder, overrides_folder, failures)
            write_rules(any_applied, new_rules, overrides_folder)
            if not any_applied:
                # we had nothing left to try.
                break
            max_trials -= 1
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
                log.info(
                    "Check out the (commited) overrides in {overrides_folder} using `git diff HEAD~1 HEAD`"
                )
        else:
            extract_sources(src_folder, failures)
            log.error(
                f"We failed to achive success in build. Read the error logs in {project_folder} and try to extend the rule set?"
            )
            log.info(f"The sources of failing packages has been extracted to {src_folder}")
            if max_trials == 0:
                log.info("We gave up because the maximum number of trials was reached")
            else:
                log.info("we gave up because we had no further rules")
            sys.exit(1)
