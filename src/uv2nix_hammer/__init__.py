import sys
import toml
import re
from pathlib import Path
import subprocess
from . import rules


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

  inputs.uv2nix.url = "{uv2nix_repo}";
  inputs.uv2nix.inputs.nixpkgs.follows = "nixpkgs";
  inputs.uv2nix_hammer_overrides.url = "{hammer_overrides_folder.absolute()}";
  inputs.uv2nix_hammer_overrides.inputs.nixpkgs.follows = "nixpkgs";
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
    pass  # TODO


def gitify(folder):
    if not (folder / ".git").exists():
        subprocess.check_call(["git", "init"], cwd=folder)
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
    )
    subprocess.run(
        ["nix", "build"],
        cwd=project_folder,
        stderr=(project_folder / f"run_{attempt_no}.log").open("w"),
    )
    return attempt_no


def remove_old_logs(project_folder):
    for fn in project_folder.glob("run_*.log"):
        fn.unlink()


def get_nix_log(drv):
    return subprocess.check_output(["nix", "log", drv], text=True)


def load_failures(project_folder, run_no):
    log_file = project_folder / f"run_{run_no}.log"
    raw = log_file.read_text()
    failed_drvs = re.findall("error: builder for '(/nix/store/[^']+)' failed", raw)
    return {drv: get_nix_log(drv) for drv in failed_drvs}


def load_existing_rules(overrides_folder, pkg, pkg_version):
    path = overrides_folder / "overrides" / pkg / pkg_version / "rules.toml"
    if path.exists():
        return toml.load(path)["rules"]
    else:
        return []


def write_combined_rules(path, rules_to_combine):
    function_arguments = set()
    function_body = ""
    for rule_name in rules_to_combine:
        rule = getattr(rules, rule_name)
        args, body = rule.apply()
        function_arguments.update(args)
        function_body += body
    path.write_text(f"""
    {{{", ".join(function_arguments)}, ...}}: {function_body}
    """)


def apply_rules(project_folder, overrides_folder, failures):
    print("apply rules", len(failures))
    any_applied = False
    rules_so_far = {}
    for drv, log in failures.items():
        print(drv)
        nix_name = drv.split("/")[-1]
        pkg_tuple = tuple(nix_name[:-4].rsplit("-")[-2:])
        rules_here = load_existing_rules(overrides_folder, *pkg_tuple)
        print(pkg_tuple)
        for rule_name in dir(rules):
            rule = getattr(rules, rule_name)
            if isinstance(rule, type):
                if not rule_name in rules_here:
                    print(f"checking rule {rule_name} in {pkg_tuple}")
                    if rule.match(log):
                        print("\t Hit!")
                        rules_here.append(rule_name)
                        any_applied = True
        rules_so_far[pkg_tuple] = rules_here

    if any_applied:
        for (pkg, version), rules_here in rules_so_far.items():
            path = overrides_folder / "overrides" / pkg / version / "rules.toml"
            toml.dump({"rules": rules_here}, path.open("w"))
            write_combined_rules(path.with_name("default.nix"), rules_here)
    return any_applied


def clear_existing_overrides(overrides_folder, target_pkg, target_pkg_version):
    default_nix = (
        overrides_folder / "overrides" / target_pkg / target_pkg_version / "default.nix"
    )
    default_nix.parent.mkdir(exist_ok=True, parents=True)
    default_nix.write_text("""{...}: old: {}""")
    rules_toml = (
        overrides_folder / "overrides" / target_pkg / target_pkg_version / "rules.toml"
    )
    rules_toml.write_text("""rules = []""")


def main():
    target_pkg = "mxp"
    target_pkg_version = "0.3"
    verify_target_on_pypi(target_pkg, target_pkg_version)

    overrides_source = "https://github.com/TyberiusPrime/uv2nix_hammer_overrides"
    uv2nix = "github:/adisbladis/uv2nix"

    target_folder = Path("hammer_build")
    target_folder.mkdir(exist_ok=True)
    project_folder = target_folder / "build"
    overrides_folder = target_folder / "overrides"

    # clone th overrides repo
    if not overrides_folder.exists():
        overrides_folder.mkdir(exist_ok=True)
        print("git cloning hammer_overrides")
        subprocess.run(["git", "clone", overrides_source, str(overrides_folder)])
        subprocess.run(
            ["git", "switch", "-c", f"{target_pkg}-{target_pkg_version}"],
            cwd=overrides_folder,
        )

    project_folder.mkdir(exist_ok=True)
    # todo: dependency tracking?
    if not (project_folder / "pyproject.toml").exists():
        write_pyproject_toml(project_folder, target_pkg, target_pkg_version)
    if not (project_folder / "uv.lock").exists():
        uv_lock(project_folder)
    # if not (project_folder / "flake.nix").exists():
    write_flake_nix(project_folder, uv2nix, overrides_folder)
    gitify(project_folder)

    remove_old_logs(project_folder)

    clear_existing_overrides(overrides_folder, target_pkg, target_pkg_version)

    max_trials = 10
    success = False
    while max_trials > 0:
        run_no = attempt_build(project_folder)
        if (project_folder / "result").exists():
            success = True
            break
        failures = load_failures(project_folder, run_no)
        if not apply_rules(project_folder, overrides_folder, failures):
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
        print("We had success building the packages. Check out the (new) overrides in {overrides_folder}")
    else:
        print("We failed to achive success in build. Read the error logs in {project_folder} and try to extend the rule set?")
        sys.exit(1)
