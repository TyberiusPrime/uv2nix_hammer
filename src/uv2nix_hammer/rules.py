from .helpers import (
    extract_pyproject_toml_from_archive,
    get_src,
    log,
    RuleOutput,
    Rule,
    drv_to_pkg_and_version,
)
from .nix_format import nix_literal


class BuildSystems(Rule):
    @staticmethod
    def normalize_build_system(bs):
        for char in ">;<=[":
            if char in bs:
                bs = bs[: bs.index(char)]
        bs = bs.replace("_", "-")
        return bs.lower()

    @classmethod
    def match(cls, drv, drv_log, opts):
        if opts is None:  # no build system yet - read pyproject.toml if available..
            opts = []
            try:
                src = get_src(drv)
                try:
                    pyproject_toml = extract_pyproject_toml_from_archive(src)
                    log.debug(f"\tgot pyproject.toml for {drv}")
                    opts = list(  # sorting is just before return
                        set(
                            [
                                cls.normalize_build_system(x)
                                for x in pyproject_toml.get("build-system", {}).get(
                                    "requires", []
                                )
                            ]
                        )
                    )
                    filtered_build_systems = [
                        "hatch-docstring-description"  # not in nixpkgs and useless-for-our-purposes-metadata anyway
                    ]
                    opts = [x for x in opts if not x in filtered_build_systems]
                    log.debug(f"\tfound build-systems: {opts}")
                except ValueError:
                    opts = []
            except ValueError:
                opts = []  # was a wheel
        if "No module named 'setuptools'" in drv_log:
            if not "setuptools" in opts:
                opts.append("setuptools")
        if "RuntimeError: Running cythonize failed!" in drv_log and "cython" in opts:
            log.debug("detected failing cython - trying cython_0")
            opts.remove("cython")
            opts.append("cython_0")
        opts = sorted(set(opts))
        return opts

    @staticmethod
    def apply(opts):
        return RuleOutput(build_inputs=opts)


class TomlRequiresPatcher(Rule):
    @staticmethod
    def match(drv, drv_log, opts):
        if "Missing dependencies:" in drv_log:
            start = drv_log[drv_log.find("Missing dependencies:") :]
            next_line = start[start.find("\n") + 1 :]
            end = next_line[: next_line.find("\n")]
            return "<" in next_line or "==" in next_line

    @staticmethod
    def apply(opts):
        return RuleOutput(
            arguments=["helpers"],
            src_attrset_parts={
                "patchPhase": """
                ${helpers.tomlreplace} pyproject.toml build-system.requires "[]"
        """
            },
        )


class NativeBuildInputs(Rule):
    @staticmethod
    def match(drv, drv_log, opts):
        if opts is None:
            opts = []
        if "No such file or directory: 'gfortran'" in drv_log:
            opts.append(nix_literal("pkgs.gfortran"))
        if "Did not find pkg-config" in drv_log:
            opts.append(nix_literal("pkgs.pkg-config"))
        if "The headers or library files could not be found for zlib," in drv_log:
            opts.append(nix_literal("pkgs.zlib.dev"))
            opts.append(nix_literal("pkgs.pkg-config"))
        return sorted(set(opts))

    @staticmethod
    def apply(opts):
        return RuleOutput(
            arguments=["pkgs"], src_attrset_parts={"nativeBuildInputs": opts}
        )


class BuildInputs(Rule):
    @staticmethod
    def match(drv, drv_log, opts):
        if opts is None:
            opts = []
        # if 'Dependency "OpenBLAS" not found,' in drv_log:
        #     opts.append(nix_literal("pkgs.blas"))
        #     opts.append(nix_literal("pkgs.lapack"))
        if "error: libhdf5.so: cannot open shared object file" in drv_log:
            opts.append(nix_literal("pkgs.hdf5"))

        if "libtbb.so.12 -> not found!" in drv_log:
            opts.append(nix_literal("pkgs.tbb_2021_11.out"))
        return sorted(set(opts))

    @staticmethod
    def apply(opts):
        return RuleOutput(
            arguments=["pkgs"],
            src_attrset_parts={"buildInputs": opts},
            wheel_attrset_parts={"buildInputs": opts},
        )


class ManualOverrides(Rule):
    @staticmethod
    def match(drv, drv_log, opts):
        pkg, version = drv_to_pkg_and_version(drv)
        if pkg == "pillow":
            return "pillow"
        return None

    @staticmethod
    def apply(opts):
        if opts == "pillow":
            return RuleOutput(
                arguments=["pkgs"],
                src_attrset_parts={
                    "preConfigure": nix_literal(
                        "pkgs.python3Packages.pillow.preConfigure"
                    )
                },
            )
        else:
            return None
