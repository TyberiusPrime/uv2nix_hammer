from os import stat
import re
from .helpers import (
    extract_pyproject_toml_from_archive,
    search_and_extract_from_archive,
    has_pyproject_toml,
    get_src,
    log,
    RuleOutput,
    RuleFunctionOutput,
    RuleOutputTriggerExclusion,
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
    def match(cls, drv, drv_log, opts, _rules_here):
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
                        "hatch-docstring-description",  # not in nixpkgs and useless-for-our-purposes-metadata anyway
                        "setuptools-scm-git-archive",  # marked as broken in nixpkgs, plugin is obsolete, setuptools-scm can do it.
                    ]
                    opts = [x for x in opts if not x in filtered_build_systems]
                    log.debug(f"\tfound build-systems: {opts}")
                except KeyError:
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
            if "setuptools-scm" in drv_log or "setuptools_scm" in drv_log:
                opts.append("setuptools-scm")
            if "setuptools-git" in drv_log or "setuptools_git" in drv_log:
                opts.append("setuptools-git")
            if "pytest-runner" in drv_log:
                opts.append("pytest-runner")
            if "pycodestyle" in drv_log:
                opts.append("pycodestyle")
            if "isort" in drv_log:
                opts.append("isort")
            if "cython" in drv_log:
                opts.append("cython")
            if "pkgconfig" in drv_log:
                opts.append("pkgconfig")
            if "pip" in drv_log:
                opts.append("pip")
            if "pbr" in drv_log:
                opts.append("pbr")
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
    def match(drv, drv_log, opts, _rules_here):
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
    def match(drv, drv_log, opts, _rules_here):
        if "Missing dependencies:" in drv_log and has_pyproject_toml(drv):
            start = drv_log[drv_log.find("Missing dependencies:") :]
            next_line = start[start.find("\n") + 1 :]
            next_line = next_line[: next_line.find("\n")]
            return [x in next_line for x in requirements_sep_chars]

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
    def match(drv, drv_log, opts, _rules_here):
        if opts is None:
            opts = []
        for q, vs in [
            ("No such file or directory: 'gfortran'", "gfortran"),
            ("but no Fortran compiler found", "gfortran"),
            ("Did not find pkg-config", "pkg-config"),
            ("No such file or directory: 'pkg-config'", "pkg-config"),
            (
                "The headers or library files could not be found for zlib",
                ["zlib.dev", "pkg-config"],
            ),
            ("zlib.h: No such file or directory", ["zlib.dev", "pkg-config"]),
            ("pkg-config is required for building", "pkg-config"),
            ('"pkg-config" command could not be found.', "pkg-config"),
            ("CMake must be installed to build from source.", "cmake"),
            ("CMake is not installed on your system!", "cmake"),
            ("checking for GTK+ - version >= 3.0.0... no", ["gtk3", "pkg-config"]),
            ("systemd/sd-daemon.h: No such file", "pkg-config"),  # cysystemd
            ("cython<1.0.0,>=0.29", "final.cython_0"),
            ("ModuleNotFoundError: No module named 'mesonpy", "meson"),
            # setuptools should have been handled by BuildSystems,
            # so something else must be wrong.
            # ("ModuleNotFoundError: No module named 'setuptools'", "final.setuptools"),
            ("gobject-introspection-1.0 found: NO", "gobject-introspection"),
            ("did not manage to locate a library called 'augeas'", "pkg-config"),
            ("pkg-config: command not found", "pkg-config"),
            ("krb5-config", "krb5"),
            ("libpyvex.so -> not found!", "final.pyvex"),
            ('#include "cairo.h"', ["pkgs.cairo.dev", "pkg-config"]),
            (
                "Specify MYSQLCLIENT_CFLAGS and MYSQLCLIENT_LDFLAGS env vars manually",
                "libmysqlclient",
            ),
            ("ModuleNotFoundError: No module named 'torch'", "final.torch"),
            ("ta_defs.h: No such file", "ta-lib"),
            ("Program 'swig' not found or not executable", ["swig"]),
            (" fatal error: ffi.h: No such file or directory", ["pkg-config"]),
            (
                "Unable to locate bz2 library needed when enabling bzip2 support",
                ["bzip2.dev", "pkg-config"],
            ),
            ("gmp.h: No such file or directory", ["pkg-config", "gmp.dev"]),
            ("ModuleNotFoundError: No module named 'pip'", "final.pip"),
            ("Could not run curl-config", "curl"),
            ("do you have the `libxml2` development package installed?", "libxml2"),
            ("cannot get XMLSec1", "pkgs.xmlsec.out"),
        ]:
            if q in drv_log:
                if isinstance(vs, list):
                    opts.extend(
                        nix_literal(("pkgs." + x) if not "." in x or ".dev" in x else x)
                        for x in vs
                    )
                else:
                    opts.append(
                        nix_literal(
                            ("pkgs." + vs) if not "." in vs or ".dev" in vs else vs
                        )
                    )

        return sorted(set(opts))

    @staticmethod
    def apply(opts):
        src_attrset = {}
        if nix_literal("pkgs.ta-lib") in opts:
            src_attrset["env"] = {
                "TA_INCLUDE_PATH": "${pkgs.ta-lib}/include",
                "TA_LIBRARY_PATH": "${pkgs.ta-lib}/lib",
            }
            if not "cython_0" in opts:
                opts.append(nix_literal("final.cython_0"))

        src_attrset["nativeBuildInputs"] = opts
        if nix_literal("pkgs.cmake") in opts or "pkgs.meson" in opts:
            src_attrset["dontUseCmakeConfigure"] = True

        args = sorted(
            set(x[len("~literal:!:") :].split(".")[0] for x in opts if "." in x)
        )
        return RuleOutput(arguments=args, src_attrset_parts=src_attrset)


class BorkedRuntimeDepsCheck(Rule):
    @staticmethod
    def match(drv, drv_log, opts, _rules_here):
        # todo: make this less generic?
        return "not satisfied by version" in drv_log

    @staticmethod
    def apply(opts):
        src_attrset = {"env": {"dontCheckRuntimeDeps": True}}
        return RuleOutput(src_attrset_parts=src_attrset)


class BuildInputs(Rule):
    @staticmethod
    def match(drv, drv_log, opts, _rules_here):
        if opts is None:
            opts = []
        # if 'Dependency "OpenBLAS" not found,' in drv_log:
        #     opts.append(nix_literal("pkgs.blas"))
        #     opts.append(nix_literal("pkgs.lapack"))
        for k, pkgs in [
            ("error: libhdf5.so: cannot open shared object file", "hdf5"),
            ("libtbb.so.12 -> not found!", "tbb_2021_11.out"),
            ("zlib.h: No such file or directory", "pkgs.zlib.out"),
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
            ("libfreetype.so.6 -> not found!", "freetype"),
            ("libGLU.so.1 -> not found!", "libGLU"),
            ("liblzma.so.5 -> not found!", "xz"),
            ("libxml2.so.2 -> not found!", "libxml2"),
            ("libSDL2-2.0.so.0 -> not found!", "SDL2"),
            ("libodbc.so.2 -> not found!", "unixODBC"),
            ("alsa/asoundlib.h", "pkgs.alsa-lib"),
            (
                "Specify MYSQLCLIENT_CFLAGS and MYSQLCLIENT_LDFLAGS env vars manually",
                "libmysqlclient",
            ),
            ("#include <ev.h>", "libev"),
            ("Is Open Babel installed?", "openbabel"),
            ("#include <bluetooth/bluetooth.h>", "bluez"),
            (" #include <boost/optional.hpp>", "boost"),
            ("/poppler-document.h: No such", "poppler"),
            ("Could not find required package: opencv.", "opencv4"),
            ("chm_lib.h: No such file", "chmlib"),  #
            ("C shared or static library 'blas' not found", "blas"),
            ("C header 'umfpack.h' not found", "suitesparse"),
            ("incdir = os.path.relpath(np.get_include())", "final.numpy"),
            ("libc.musl-x86_64.so.1", "musl"),
            (" fatal error: ffi.h: No such file or directory", "libffi"),
            ("lber.h: No such file", ["pkgs.openldap.dev", "pkg-config", "cyrus_sasl"]),
            ("gmp.h: No such file or directory", ["gmp"]),
            ("lzma.h: No such file", "pkgs.xz.dev"),
            ('#include "portaudio.h"', ["portaudio"]),
            ("cannot get XMLSec1", "pkgs.xmlsec.dev"),
            # (" RequiredDependencyException: pangocairo", "pango"),
        ]:
            if k in drv_log:
                if isinstance(pkgs, str):
                    pkgs = [pkgs]
                for pkg in pkgs:
                    if not "." in pkg or pkg.startswith("cudaPackages"):
                        opts.append(nix_literal(f"pkgs.{pkg}"))
                    else:
                        opts.append(nix_literal(pkg))

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
        needs_master = nix_literal("pkgs.cudaPackages.cudnn") in opts
        if needs_master:
            log.debug("Switching to master because of cuda")
        return RuleOutput(
            arguments=["pkgs"],
            src_attrset_parts={"buildInputs": opts, "env": env},
            wheel_attrset_parts={"buildInputs": opts},
            # nixpkgs 24.05 has no cudnn 9.x
            requires_nixpkgs_master=needs_master,
        )


# can't get that to work so far... also see angr.
# class OtherPythonPackagesLDSearchPath(Rule):
#     @staticmethod
#     def match(drv, drv_log, opts, rules_here):
#         if "libboost_python312.so.1.83.0 -> not found!" in drv_log:
#             return "cmeel-boost"

#     @staticmethod
#     def apply(opts):
#         if opts == "cmeel-boost":
#             return RuleOutput(
#                 arguments=["final"],
#                 src_attrset_parts={
#                     "libs": "${final.cmeel-boost}/lib/python3.12/site-packages/cmeel.prefix/lib"
#                 },
#             )


class MasterForTorch(Rule):
    always_reapply = True

    @staticmethod
    def match(drv, drv_log, opts, rules_here):
        return nix_literal("pkgs.cudaPackages.cudnn") in rules_here.get(
            "BuildInputs", []
        )

    @staticmethod
    def apply(opts):
        return RuleOutput(requires_nixpkgs_master=True)


manual_rule_path = None  # set from outside


class ManualOverrides(Rule):
    @staticmethod
    def match(drv, drv_log, opts, _rules_here):
        pkg, version = drv_to_pkg_and_version(drv)
        if pkg == "pillow":
            return "pillow"
        # no need for version searching here. If you need to reuse the rules for other versions
        # drop a symlink.
        log.debug(
            f"Manual path would be {(manual_rule_path / pkg / version / 'default.nix')}"
        )
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
    def match(drv, drv_log, opts, _rules_here):
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
    def match(drv, drv_log, opts, _rules_here):
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
    def match(drv, drv_log, opts, _rules_here):
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
    def match(drv, drv_log, opts, _rules_here):
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
    def match(drv, drv_log, opts, _rules_here):
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


class DowngradeNumpy(Rule):
    """Downgrade numpy when it's a clear >= 2.0 not suppported case"""

    @staticmethod
    def match(drv, drv_log, opts, _rules_here):
        if (
            "'int_t' is not a type identifier" in drv_log and "np.int_t" in drv_log
            ):
            return "<2"
        elif "No module named 'numpy.distutils'" in drv_log:
            return "<1.22"


        # or ("numpy/arrayobject.h: No such file" in drv_log)

    @staticmethod
    def apply(opts):
        if opts is True:
            opts = "<2"
        return RuleOutput(dep_constraints={"numpy": opts})


class DowngradePython(Rule):
    """Downgrade numpy when it's a clear >= 2.0 not suppported case"""

    @staticmethod
    def match(drv, drv_log, opts, _rules_here):
        if "3.12" in drv_log and "No module named 'distutils'" in drv_log:
            return "3.11"
        if "greenlet-1.1.0" in drv_log:
            return "3.10"
        if (
            "return kh_float64_hash_func(val.real)^kh_float64_hash_func(val.imag);"
            in drv_log
        ):
            return "3.10"  # old pandas 1.5.3
        if "ModuleNotFoundError: No module named 'distutils'" in drv_log:
            return "3.11"
        if "fatal error: longintrepr.h: " in drv_log:
            return "3.10"
        if "AttributeError: fcompiler. Did you mean: 'compiler'?" in drv_log:
            # that's trying to compile numpy 1.22, o
            return "3.10"

    @staticmethod
    def apply(opts):
        log.debug(f"Downgrading to python {opts}")
        return RuleOutput(python_downgrade=opts)


class IsPython2Only(Rule):
    @staticmethod
    def match(drv, drv_log, opts, _rules_here):
        if "is Python 2 only." in drv_log:
            pkg_tuple = drv_to_pkg_and_version(drv)
            return f"Is_python2_only: {pkg_tuple}"
        if (
            "PyFloat_FromString(str, NULL);" in drv_log
        ):  # points to a long long ago c api.
            pkg_tuple = drv_to_pkg_and_version(drv)
            return f"Is_python2_only (from c code): {pkg_tuple}"
        if "NameError: name 'execfile' is not defined" in drv_log:
            pkg_tuple = drv_to_pkg_and_version(drv)
            return f"Is_python2_only (uses execfile): {pkg_tuple}"
        if "Missing dependencies" in drv_log and "nose" in drv_log:
            return f"Is_python2_only (required nose): {drv_to_pkg_and_version(drv)}"
        if "NameError: name 'file' is not defined" in drv_log:
            return (
                f"Is_python2_only (file is not defined): {drv_to_pkg_and_version(drv)}"
            )

    @staticmethod
    def apply(opts):
        return RuleOutputTriggerExclusion(opts)


class Rust(Rule):
    @classmethod
    def match(cls, drv, drv_log, opts, rules_here):
        result = None
        if "setuptools_rust" in drv_log or "setuptools-rust" in rules_here.get(
            "BuildSystems", []
        ):
            result = "setuptools_rust"
        elif "maturin" in drv_log or "maturin" in rules_here.get("BuildSystems", []):
            result = "maturin"
        return result

    @staticmethod
    def apply(opts):
        return RuleFunctionOutput("""
                                  pkgs.lib.optionalAttrs (old.format or "sdist" != "wheel") (helpers.standardMaturin {} old)
                                  """)

    @staticmethod
    def extract(drv, target_folder):
        target_path = target_folder / "Cargo.lock"
        target_path.parent.mkdir(parents=True, exist_ok=True)
        src = get_src(drv)
        try:
            cargo_lock = search_and_extract_from_archive(src, "Cargo.lock")
        except KeyError:
            raise ValueError("implement cargo lock builder")

        target_path.write_text(cargo_lock)


class Enum34(Rule):
    @staticmethod
    def match(drv, drv_log, opts, _rules_here):
        if "module 'enum' has no attribute 'global_enum'" in drv_log:
            return f"Requires enum34, a pre python 3.6 thing."

    @staticmethod
    def apply(opts):
        return RuleOutputTriggerExclusion(opts)


class PythonTooNew(Rule):
    # and we need to exclude this from our builds
    # see DowngradePython for the other case
    @staticmethod
    def match(drv, drv_log, opts, _rules_here):
        if "type object 'Callable' has no attribute '_abc_registry'" in drv_log:
            return f"requires pypi typing, but typing has been built in since 3.6"
        if "sqlcipher/sqlite3.h:" in drv_log:
            return f"requires sqlcipher, which is disabled since python 3.9"
        if "module 'typing' has no attribute '_ClassVar'" in drv_log:
            return "requires dataclasses from pypi (so python before 3.6)"
        if "This backport is meant only for Python 2." in drv_log:
            pkg_tuple = drv_to_pkg_and_version(drv)
            return "This backport is meant only for Python 2. {pkg_tuple}"
        if " error: invalid use of incomplete typedef ‘PyInterpreterState" in drv_log:
            return (
                "invalid use of incomplete typedef ‘PyInterpreterState’ (python <3.8?)"
            )
        if "Supported interpreter versions: 3.5, 3.6, 3.7, 3.8\n" in drv_log:
            pkg_tuple = drv_to_pkg_and_version(drv)
            return f"Supported interpreter versions: 3.5, 3.6, 3.7, 3.8: {pkg_tuple}"

    @staticmethod
    def apply(opts):
        return RuleOutputTriggerExclusion(opts)


class MacOnly(Rule):
    @staticmethod
    def match(drv, drv_log, opts, _rules_here):
        if "PyObjC requires macOS to build" in drv_log:
            pkg_tuple = drv_to_pkg_and_version(drv)
            return f"PyObjC requires macOS to build: {pkg_tuple}"

    @staticmethod
    def apply(opts):
        return RuleOutputTriggerExclusion(opts)
