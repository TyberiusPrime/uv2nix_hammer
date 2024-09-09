import re
from .helpers import (
    extract_pyproject_toml_from_archive,
    get_src,
    log,
    RuleOutput,
    Rule,
    drv_to_pkg_and_version,
    get_release_date,
)
import datetime
from .nix_format import nix_literal


requirements_sep_chars = ">;<=[~"


class BuildSystems(Rule):
    @staticmethod
    def normalize_build_system(bs):
        for char in requirements_sep_chars:
            if char in bs:
                bs = bs[: bs.index(char)]
        bs = bs.replace("_", "-")
        return bs.lower().strip()

    @classmethod
    def match(cls, drv, drv_log, opts):
        # the cython3 thing is a debacle.
        if opts and "cython" in opts:  # -> we already tried it with cython3
            pkg, version = drv_to_pkg_and_version(drv)
            release_date = get_release_date(pkg, version)
            # log.debug(
            #     f"Tried cython(3) and failed - checking release date: {release_date}"
            # )
            if (
                release_date
                < datetime.datetime(
                    2023, 7, 17
                )  # if it's older than the cython3 release date...
                # or "gcc' failed with exit code" in drv_log
            ):
                log.debug("\tTrying with cython_0")
                opts.remove("cython")
                opts.append("cython_0")

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
        if "Missing dependencies:" in drv_log:
            if "setuptools-scm" in drv_log:
                opts.append("setuptools-scm")
            if "pytest-runner" in drv_log:
                opts.append("pytest-runner")
            if "pycodestyle" in drv_log:
                opts.append("pycodestyle")
            if "isort" in drv_log:
                opts.append("isort")

        opts = sorted(set(opts))
        if "poetry" in opts:
            opts.remove("poetry")
            opts.append("poetry-core")

        return opts

    @staticmethod
    def apply(opts):
        return RuleOutput(build_inputs=opts)


class PoetryMasonry(Rule):
    @staticmethod
    def match(drv, drv_log, opts):
        return "ModuleNotFoundError: No module named 'poetry.masonry'" in drv_log

    @staticmethod
    def apply(opts):
        return RuleOutput(
            src_attrset_parts={
                "postPatch": """
                    substituteInPlace pyproject.toml --replace-fail "poetry.masonry.api" "poetry.core.masonry.api"
                """
            },
        )


class TomlRequiresPatcher(Rule):
    @staticmethod
    def match(drv, drv_log, opts):
        if "Missing dependencies:" in drv_log:
            start = drv_log[drv_log.find("Missing dependencies:") :]
            next_line = start[start.find("\n") + 1 :]
            next_line = next_line[: next_line.find("\n")]
            return any((x in next_line for x in requirements_sep_chars))

    @staticmethod
    def apply(opts):
        return RuleOutput(
            arguments=["helpers"],
            src_attrset_parts={
                "postPatch": """
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
        if ("The headers or library files could not be found for zlib," in drv_log) or (
            "zlib.h: No such file or directory" in drv_log
        ):
            opts.append(nix_literal("pkgs.zlib.dev"))
            opts.append(nix_literal("pkgs.pkg-config"))
        if "pkg-config is required for building":
            opts.append(nix_literal("pkgs.pkg-config"))
        if "CMake must be installed to build from source.":
            opts.append(nix_literal("pkgs.cmake"))
        return sorted(set(opts))

    @staticmethod
    def apply(opts):
        src_attrset = {"nativeBuildInputs": opts}
        if nix_literal("pkgs.cmake") in opts:
            src_attrset["dontUseCmakeConfigure"] = True
        return RuleOutput(arguments=["pkgs"], src_attrset_parts=src_attrset)


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
        if "zlib.h: No such file or directory" in drv_log:
            opts.append(nix_literal("pkgs.zlib.out"))
        if "No package 'libswscale' found" in drv_log:
            opts.append(nix_literal("pkgs.ffmpeg"))
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


class MissingEmptyFiles(Rule):
    @staticmethod
    def match(drv, drv_log, opts):
        if hits := re.findall(
            "No such file or directory: '([^']*(requirements.txt))'", drv_log
        ):
            return [x[0] for x in hits]

    @staticmethod
    def apply(opts):
        return RuleOutput(
            src_attrset_parts={
                "postPatch": "\n".join((f"touch {file}" for file in opts))
            },
        )


class VersioneerBitRot(Rule):
    @staticmethod
    def match(drv, drv_log, opts):
        if "module 'configparser' has no attribute 'SafeConfigParser'." in drv_log:
            return True

    @staticmethod
    def apply(opts):
        return RuleOutput(
            arguments=["final", "pkgs"],
            src_attrset_parts={
                "postPatch": nix_literal("""
                 pkgs.lib.optionalString (!(final.pythonOlder "3.12")) 
                 ''
                 if [ -e setup.py ]; then
                      substituteInPlace setup.py --replace-quiet "versioneer.get_version()" "'${old.version}'" \\
                        --replace-quiet "cmdclass=versioneer.get_cmdclass()," "" \\
                        --replace-quiet "cmdclass=versioneer.get_cmdclass()" ""
                 fi
                 ''
                """)
            },
        )
