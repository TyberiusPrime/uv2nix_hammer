import re
from .helpers import (
    extract_pyproject_toml_from_archive,
    has_pyproject_toml,
    get_src,
    log,
    RuleOutput,
    RuleFunctionOutput,
    Rule,
    drv_to_pkg_and_version,
    get_release_date,
)
import datetime
from pathlib import Path
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
                (
                    release_date
                    < datetime.datetime(
                        2023, 7, 17
                    )  # if it's older than the cython3 release date...
                )
                or "Cython.Compiler.Errors.CompileError:" in drv_log
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
            # log.error(f"Missing dependencies - {drv}")
            if "setuptools-scm" in drv_log:
                opts.append("setuptools-scm")
            if "setuptools_scm" in drv_log:
                opts.append("setuptools-scm")
            if "pytest-runner" in drv_log:
                opts.append("pytest-runner")
            if "pycodestyle" in drv_log:
                opts.append("pycodestyle")
            if "isort" in drv_log:
                opts.append("isort")
            if "cython" in drv_log:
                opts.append("cython")
        if (
            "Cython.Compiler.Errors.CompileError:" in drv_log
            and not "cython" in opts
            and not "cython_0" in opts
        ):
            opts.append("cython")

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
        if "Missing dependencies:" in drv_log and has_pyproject_toml(drv):
            start = drv_log[drv_log.find("Missing dependencies:") :]
            next_line = start[start.find("\n") + 1 :]
            next_line = next_line[: next_line.find("\n")]
            return [(x in next_line for x in requirements_sep_chars)]

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
        for q, vs in [
            ("No such file or directory: 'gfortran'", "gfortran"),
            ("Did not find pkg-config", "pkg-config"),
            (
                "The headers or library files could not be found for zlib",
                ["zlib.dev", "pkg-config"],
            ),
            ("zlib.h: No such file or directory", ["zlib.dev", "pkg-config"]),
            ("pkg-config is required for building", "pkg-config"),
            ('"pkg-config" command could not be found.', "pkg-config"),
            ("CMake must be installed to build from source.", "cmake"),
            ("checking for GTK+ - version >= 3.0.0... no", ["gtk3", "pkg-config"]),
            ("systemd/sd-daemon.h: No such file", "pkg-config"),  # cysystemd
            ("cython<1.0.0,>=0.29", "final.cython_0"),
            ("ModuleNotFoundError: No module named 'mesonpy", "meson"),
            ("gobject-introspection-1.0 found: NO", "gobject-introspection"),
            ("did not manage to locate a library called 'augeas'", "pkg-config"),
            ("pkg-config: command not found", "pkg-config"),
        ]:
            if q in drv_log:
                if isinstance(vs, list):
                    opts.extend(
                        nix_literal(("pkgs." + x) if not "." in x else x) for x in vs
                    )
                else:
                    opts.append(nix_literal(("pkgs." + vs) if not "." in vs else vs))

        return sorted(set(opts))

    @staticmethod
    def apply(opts):
        src_attrset = {"nativeBuildInputs": opts}
        if nix_literal("pkgs.cmake") in opts or "pkgs.meson" in opts:
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
        for k, pkg in [
            ("error: libhdf5.so: cannot open shared object file", "hdf5"),
            ("libtbb.so.12 -> not found!", "tbb_2021_11.out"),
            ("zlib.h: No such file or directory", "zlib.out"),
            ("No package 'libswscale' found", "ffmpeg"),
            ("libtensorflow_framework.so.2 -> not found!", "libtensorflow"),
            ("libnvJitLink.so.12 -> not found!", "cudaPackages.libnvjitlink"),
            ("libcublas.so.12 -> not found!", "cudaPackages.libcublas"),
            ("libcusparse.so.12 -> not found!", "cudaPackages.libcusparse"),
            ("libcusolver.so.11 -> not found", "cudaPackages_11.libcusolver"),
            ("libcudart.so.12 -> not found", "cudaPackages.cuda_cudart"),
            ("libnvrtc.so.12 -> not found!", "cudaPackages.cuda_nvrtc"),
            ("libcupti.so.12 -> not found!", "cudaPackages.cuda_cupti"),
            ("libcufft.so.11 -> not found!", "cudaPackages.libcufft"),
            ("libnvToolsExt.so.1 -> not found!", "cudaPackages.cuda_nvtx"),
            ("libcurand.so.10 -> not found!", "cudaPackages.libcurand"),
            (
                "libcudnn.so.9 -> not found!",
                "cudaPackages.cudnn",
            ),  # that means we also need mater...
            ("libnccl.so.2 -> not found!", "cudaPackages.nccl"),
            (
                "ld: cannot find -lncurses:",
                "ncurses",
            ),  # todo: is that right? that's what poetry2nix did for readline/gnureadline, but it's supposed to be statically linked?
            ("slurm/spank.h: No such file or directory", "slurm"),
            ("systemd/sd-daemon.h: No such file", "systemd"),
            ("libglib-2.0.so.0 -> not found!", "glib"),
            ("libX11.so.6 -> not found!", "xorg.libX11"),
            ("libnss3.so -> not found!", "nss"),
            ("libnssutil3.so -> not found!", "nss"),
            ("libnspr4.so -> not found!", "nspr"),
            ("No package 'libsystemd' found", "systemd"),
            ('Dependency "cairo" not found,', "cairo"),
            ("did not manage to locate a library called 'augeas'", "augeas"),
        ]:
            if k in drv_log:
                opts.append(nix_literal(f"pkgs.{pkg}"))

        return sorted(set(opts))

    @staticmethod
    def apply(opts):
        if "pkgs.slurm" in opts:
            env = {
                "SLURM_LIB_DIR": "${lib.getLib slurm}/lib",
                "SLURM_INCLUDE_DIR": "${lib.getDev slurm}/include",
            }
        else:
            env = {}
        return RuleOutput(
            arguments=["pkgs"],
            src_attrset_parts={"buildInputs": opts, "env": env},
            wheel_attrset_parts={"buildInputs": opts},
            # nixpkgs 24.05 has no cudnn 9.x
            requires_nixpkgs_master="pkgs.cudaPackages.cudnn" in opts,
        )


manual_rule_path = None  # set from outside


class ManualOverrides(Rule):
    @staticmethod
    def match(drv, drv_log, opts):
        pkg, version = drv_to_pkg_and_version(drv)
        if pkg == "pillow":
            return "pillow"
        # no need for version searching here. If you need to reuse the rules for other versions
        # drop a symlink.
        log.debug(f"Looking for {(manual_rule_path / pkg / version / 'default.nix')}")
        if (manual_rule_path / pkg / version / "default.nix").exists():
            return "__file__:" + pkg + "/" + version + "/default.nix"
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
        if opts.startswith("__file__:"):
            fn = opts[len("__file__:") :]
            return RuleFunctionOutput((manual_rule_path / fn).read_text())
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


class RemovePropagatedBuildInputs(Rule):
    @staticmethod
    def match(drv, drv_log, opts):
        pass

    @staticmethod
    def apply(opts):
        return RuleOutput(
            arguments=["final", "helpers", "pkgs"],
            src_attrset_parts={
                "propagatedBuildInputs": nix_literal(f"""
                  (helpers.removePackagesByName
                    (old.propagatedBuildInputs or [ ])
                    (pkgs.lib.optionals (final ? {opts}) [ final.{opts} ]))
            """)
            },
            wheel_attrset_parts={
                "propagatedBuildInputs": nix_literal(f"""
                  (helpers.removePackagesByName
                    (old.propagatedBuildInputs or [ ])
                    (pkgs.lib.optionals (final ? {opts}) [ final.{opts} ]))
            """)
            },
        )


class RefindBuildDirectory(Rule):
    @staticmethod
    def match(drv, drv_log, opts):
        return (
            "does not appear to be a Python project: no pyproject.toml or setup.py"
            in drv_log
        )

    @staticmethod
    def apply(opts):
        return RuleOutput(
            src_attrset_parts={
                "preBuild": """
                cd /build
                cd ${old.pname}-${old.version}
                """
            },  # will fail if there's multiple directories
        )


class Torch(Rule):
    @staticmethod
    def match(drv, drv_log, opts):
        return "libc10_cuda.so -> not found!" in drv_log

    @staticmethod
    def apply(opts):
        return RuleOutput(
            arguments=["final", "pkgs"],
            wheel_attrset_parts={
                "autoPatchelfIgnoreMissingDeps": True,
                # (no patchelf on darwin, since no elves there.)
                "preFixup": nix_literal("""pkgs.lib.optionals (!pkgs.stdenv.isDarwin) ''
          addAutoPatchelfSearchPath "${final.torch}/${final.python.sitePackages}/torch/lib"
        ''"""),
            },  # will fail if there's multiple directories
        )
