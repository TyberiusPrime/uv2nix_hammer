from os import stat
import subprocess
import tempfile
import re
from .helpers import (
    drv_to_pkg_and_version,
    extract_pyproject_toml_from_archive,
    extract_source,
    get_release_date,
    get_src,
    has_pyproject_toml,
    log,
    Rule,
    RuleFunctionOutput,
    RuleOutput,
    RuleOutputCopyFile,
    RuleOutputTriggerExclusion,
    search_in_archive,
    search_and_extract_from_archive,
)
import datetime
from pathlib import Path
from packaging.version import Version
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
        pkg, version = drv_to_pkg_and_version(drv)
        if opts and "cython" in opts:  # -> we already tried it with cython3
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
                or "Cython<3,>=0.29.16" in drv_log
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
                    # log.debug(f"\tgot pyproject.toml for {drv}")
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
                except KeyError:
                    opts = []
            except ValueError:
                opts = []  # was a wheel
        if "No module named 'setuptools'" in drv_log:
            if not "setuptools" in opts:
                opts.append("setuptools")
        if "No module named pip" in drv_log and not "pip" in opts:
            opts.append("pip")
        if "RuntimeError: Running cythonize failed!" in drv_log and "cython" in opts:
            log.debug("detected failing cython - trying cython_0")
            opts.remove("cython")
            opts.append("cython_0")
        if "Missing dependencies:" in drv_log:
            lines = [
                x.strip()
                for x in drv_log[drv_log.find("Missing dependencies:") :].split("\n")
            ]
            # log.error(f"Missing dependencies - {drv}")
            if "setuptools-scm" in lines or "setuptools_scm" in lines:
                opts.append("setuptools-scm")
            if "setuptools-git" in lines or "setuptools_git" in lines:
                opts.append("setuptools-git")
            # if "setuptools-git-version" in drv_log:
            #     opts.append("setuptools-git-version") # currently not in nixpkgs
            if "pytest-runner" in lines:
                opts.append("pytest-runner")
            if "pycodestyle" in lines:
                opts.append("pycodestyle")
            if "isort" in lines:
                opts.append("isort")
            if "cython" in lines or "Cython" in lines:
                opts.append("cython")
            if "pip" in lines:
                opts.append("pip")
            if "pbr" in lines:
                opts.append("pbr")
            if "cffi" in lines:
                opts.append("cffi")
            if "wheel" in lines:
                opts.append("wheel")
        if "cppyy-cling" in drv_log:
            opts.append("cppyy-cling")
        if "cppyy-backend" in drv_log:
            opts.append("cppyy-backend")
        opts = [x for x in opts if x != pkg]

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

        while "cython" in opts and "cython_0" in opts:
            opts.remove("cython")
        filtered_build_systems = [
            "hatch-docstring-description",  # not in nixpkgs and useless-for-our-purposes-metadata anyway
            "setuptools-scm-git-archive",  # marked as broken in nixpkgs, plugin is obsolete, setuptools-scm can do it.
            "maturin",  # handled by rust below... todo: setuptools-rust
        ]
        opts = [x for x in opts if not x in filtered_build_systems]
        log.debug(f"\tfound build-systems: {opts} (after filtering)")

        return opts

    @staticmethod
    def apply(opts):
        return RuleOutput(build_systems=opts)


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

        def add_pkgs(x):
            do_add = not "." in x
            do_add |= ".dev" in x
            do_add |= "cudaPackages." in x
            if x.startswith("pkgs"):
                do_add = False
            if do_add:
                return "pkgs." + x
            else:
                return x

        for q, vs in [
            ("No such file or directory: 'gfortran'", "gfortran"),
            ("but no Fortran compiler found", "gfortran"),
            ("No CMAKE_Fortran_COMPILER could be found.", "gfortran"),
            ("Did not find pkg-config", "pkg-config"),
            ("pkgconfig", "pkg-config"),
            ("pkg-config not found", "pkg-config"),
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
            ("Cannot find CMake executable", "cmake"),
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
            ("Can not locate liberasurecode.so.1", "pkgs.liberasurecode.dev"),
            ("Error finding javahome on linux", "pkgs.openjdk"),
            ("cuda.h: No such file", "cudaPackages.cuda_cudart"),
            ("sndfile.h: No such file", ["pkgs.libsndfile.dev", "pkg-config"]),
            ("No such file or directory: 'gdal-config'", "gdal"),
            ("No such file or directory: 'which'", "which"),
            ("#include <xc.h>", "libxc"),
            ("#include <notmuch.h>", "notmuch"),
            (
                "#include <xkbcommon/xkbcommon.h>",
                ["pkgs.libxkbcommon.out", "pkgs.libxkbcommon.dev", "pkg-config"],
            ),
            ("cannot find -lvapoursynth", "vapoursynth"),
            ("PyAPI_FUNC(PyCodeObject *) PyCode_New(", "final.cython_0"),
            ("pcap.h: No such file", "libpcap"),
            ("lzo1.h: No such file", "lzo"),
            # (
            #     re.compile(
            #         "do not know how to unpack source archive [^.]+.zip",
            #     ),
            #     "unzip",
            # ),
        ]:
            is_str = isinstance(q, str)
            if (is_str and q in drv_log) or (not is_str and q.search(drv_log)):
                if not isinstance(vs, list):
                    vs = [vs]
                for x in vs:
                    opts.append(nix_literal(add_pkgs(x)))

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
            ("libtbb.so.12 -> not found!", "pkgs.tbb_2021_11.out"),
            ("libtbb.so.2 -> not found!", "pkgs.tbb.out"),
            ("zlib.h: No such file or directory", "pkgs.zlib.out"),
            ("No package 'libswscale' found", "ffmpeg"),
            ("libtensorflow_framework.so.2 -> not found!", "libtensorflow"),
            ("libnvJitLink.so.12 -> not found!", "cudaPackages.libnvjitlink"),
            ("libcublas.so.12 -> not found!", "cudaPackages.libcublas"),
            ("libcublas.so.11 -> not found!", "cudaPackages_11.libcublas"),
            ("libcusparse.so.12 -> not found!", "cudaPackages.libcusparse"),
            ("libcusparse.so.11 -> not found!", "cudaPackages_11.libcusparse"),
            ("libcusolver.so.11 -> not found", "cudaPackages_11.libcusolver"),
            ("libcudart.so.12 -> not found", "cudaPackages.cuda_cudart"),
            ("libcudart.so.11.0 -> not found", "cudaPackages_11.cuda_cudart"),
            ("libnvrtc.so.12 -> not found!", "cudaPackages.cuda_nvrtc"),
            ("libcupti.so.12 -> not found!", "cudaPackages.cuda_cupti"),
            ("libcufft.so.11 -> not found!", "cudaPackages.libcufft"),
            ("libnvToolsExt.so.1 -> not found!", "cudaPackages.cuda_nvtx"),
            ("libcurand.so.10 -> not found!", "cudaPackages.libcurand"),
            (
                "libcudnn.so.9 -> not found!",
                "cudaPackages.cudnn",
            ),  # that means we also need mater...
            ("cuda.h: No such file", "cudaPackages.cuda_cudart"),
            # ("cudaProfiler.h", "cudaPackages.cuda_nvml_dev"),
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
            ("Can not locate liberasurecode.so.1", "pkgs.liberasurecode.out"),
            ("Error finding javahome on linux", "pkgs.openjdk"),
            ("sndfile.h: No such file", "pkgs.libsndfile.out"),
            ("No such file or directory: 'gdal-config'", "gdal"),
            ("cannot find -lnotmuch:", "notmuch"),
            ("#include <xkbcommon/xkbcommon.h>", "pkgs.libxkbcommon.out"),
            ("libpyvex.so -> not found", "final.pyvex"),  # wheel doesn't declare it...
            ("libpam.so.0 -> not found!", "linux-pam"),
            ("libcrypt.so.1 -> not found!", "libxcrypt-legacy"),
            # (" RequiredDependencyException: pangocairo", "pango"),
        ]:
            if k in drv_log:
                if isinstance(pkgs, str):
                    pkgs = [pkgs]
                for pkg in pkgs:
                    if not pkg.startswith("~literal:!:"):
                        if not "." in pkg or pkg.startswith("cudaPackages"):
                            opts.append(nix_literal(f"pkgs.{pkg}"))
                        else:
                            opts.append(nix_literal(pkg))
                    else:
                        opts.append(pkg)

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
        fixups = ""
        for pkg in opts:
            if pkg.startswith("~literal:!:final."):
                pkg_str = pkg[len("~literal:!:final.") :]
                fixups += f"addAutoPatchelfSearchPath ${{final.{pkg_str}}}/${{final.python.sitePackages}}/{pkg_str}/lib\n"
        src_attr_parts = {"buildInputs": opts, "env": env}
        wheel_attr_parts = {"buildInputs": opts}
        if fixups:
            wheel_attr_parts["preFixup"] = fixups
            # src_attr_parts["libs"] = libs
        return RuleOutput(
            arguments=["pkgs"],
            src_attrset_parts=src_attr_parts,
            wheel_attrset_parts=wheel_attr_parts,
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
        p = manual_rule_path / pkg / version / "default.nix"
        log.debug(
            f"Manual path would be {p} "
            + ("(present)" if p.exists() else "(not present)")
        )
        if (manual_rule_path / pkg / version / "default.nix").exists():
            return "__file__:" + pkg + "/" + version + "/default.nix"
        return None

    @staticmethod
    def apply(opts):
        if opts == "pillow":  # todo: turn into a default.nix
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


class ManualOverrideAdditionalFiles(Rule):
    @staticmethod
    def match(drv, drv_log, opts, _rules_here):
        pkg, version = drv_to_pkg_and_version(drv)
        try:
            files = [
                x
                for x in (manual_rule_path / pkg / version).glob("*")
                if x.name != "default.nix"
            ]
            return files
        except FileNotFoundError:
            return None

    def apply(opts):
        if opts:
            return RuleOutputCopyFile(opts)


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
        if "'int_t' is not a type identifier" in drv_log and "np.int_t" in drv_log:
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
    """Downgrade python if necessary"""

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
        if "ModuleNotFoundError: No module named 'imp'" in drv_log:
            return "3.11"
        if "only versions >=3.6,<3.10 are supported." in drv_log:
            return "3.9"
        if "pygame" in drv:
            pkg_tuple = drv_to_pkg_and_version(drv)
            version = pkg_tuple[1]
            if Version(version) <= Version("2.5.2"):
                return "3.11"
        if "cannot import name 'build_py_2to3' from 'distutils" in drv_log:
            return "3.9"

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
        if "except OSError, e:" in drv_log:
            return f"Is_python2_only (except OSError, e): {drv_to_pkg_and_version(drv)}"

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
        opts, extract_result = opts
        needed_patch = extract_result
        # todo: discern maturin & setuptoolsRust
        if needed_patch:
            return RuleFunctionOutput("""
              pkgs.lib.optionalAttrs (old.format or "sdist" != "wheel") (
              helpers.standardMaturin {
              furtherArgs = {
                  postPatch = old.postPatch or "" + ''
                  cp ${./Cargo.lock} Cargo.lock
                  '';
              };
              } old)
                                  """)

        else:
            return RuleFunctionOutput("""
                                  pkgs.lib.optionalAttrs (old.format or "sdist" != "wheel") (helpers.standardMaturin {} old)
                                  """)

    @staticmethod
    def extract(drv, target_folder):
        target_path = target_folder / "Cargo.lock"
        target_path.parent.mkdir(parents=True, exist_ok=True)
        src = get_src(drv)
        try:
            cargo_lock, needed_patch = (
                search_and_extract_from_archive(src, "Cargo.lock"),
                False,
            )
        except KeyError:
            cargo_lock, needed_patch = Rust.build_missing_cargo_lock(drv, src), True
            # raise ValueError(f"implement cargo lock builder. Derivation was {drv}")

        target_path.write_text(cargo_lock)
        return str(target_path) if needed_patch else None

    def build_missing_cargo_lock(drv, src):
        pkg, version = drv_to_pkg_and_version(drv)
        log.info(f"Creating a missing Cargo.lock for {pkg}-{version}")
        tf = tempfile.TemporaryDirectory(delete=True)
        extract_source(src, tf.name)
        cargo_tomls = list(Path(tf.name).rglob("Cargo.toml"))
        if not cargo_tomls:
            raise ValueError("No Cargo.toml found")
        cargo_tomls.sort(
            key=lambda x: len(str(x))
        )  # shortest path first, just like search_and_extract_from_archive
        cargo_toml = cargo_tomls[0]
        subprocess.check_call(
            [
                "nix",
                "shell",
                "github:/nixos/nixpkgs/master#cargo",
                "-c",
                "cargo",
                "check",
            ],
            cwd=cargo_toml.parent,
        )
        return (cargo_toml.with_name("Cargo.lock")).read_text()


class MaturinBitRot(Rule):
    @staticmethod
    def match(drv, drv_log, opts, rules_here):
        if (
            "The following metadata fields in `package.metadata.maturin` section of Cargo.toml are removed since maturin 0.14.0"
            in drv_log
        ):
            src = get_src(drv)
            cargo_toml_path = search_in_archive(src, "Cargo.toml")
            return "/".join(cargo_toml_path.split("/")[1:])

    @staticmethod
    def apply(opts):
        return RuleOutput(
            arguments=["helpers"],
            src_attrset_parts={
                "postPatch": f"""
                ${{helpers.tomlremove}} {opts} package.metadata.maturin
        """
            },
        )


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
        if (
            "AttributeError: module 'distutils.util' has no attribute 'run_2to3'"
            in drv_log
        ):
            pkg_tuple = drv_to_pkg_and_version(drv)
            return f"distutils.util has no run_2to3: {pkg_tuple}"
        if "setup command: use_2to3 is invalid." in drv_log:
            pkg_tuple = drv_to_pkg_and_version(drv)
            return "setuptools too new, setup command: use_2to3 is invalid. {pkg_tuple}"

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


class QTDontWrap(Rule):
    @staticmethod
    def match(drv, drv_log, opts, _rules_here):
        return (
            "Error: wrapQtAppsHook is not used, and dontWrapQtApps is not set."
            in drv_log
        )

    @staticmethod
    def apply(opts):
        return RuleOutput(
            wheel_attrset_parts={"dontWrapQtApps": True},
            src_attrset_parts={"dontWrapQtApps": True},
        )


class MissingSetParts:
    """When you need something that's not in the pyproject.toml or default set yet.

    This isn't actually a rule, since we run it on stderr, not on a derivation log

    """

    @staticmethod
    def match(drv, drv_log, opts, _rules_here):
        if opts is None:
            opts = {}
        if re.search("attribute '[^']+' missing", drv_log):
            log.warn("Missing attribute in derivation - trying to patch it in")
            # I am looking for final.something where in the next line there's a ^ pointing iat it.
            for hit in re.finditer("final\.[a-z0-9A-Z-]+", drv_log):
                log.debug(f"Found a hit {hit}")
                last_newline = max(0, drv_log.rfind("\n", 0, hit.span()[0]) + 1)
                next_newline = drv_log.find("\n", hit.span()[1])
                this_line = drv_log[last_newline:next_newline]
                eol = drv_log.find("\n", next_newline + 1)
                if eol == -1:
                    eol = None
                next_line = drv_log[next_newline + 1 : eol]
                caret_pos = next_line.find("^")
                if hit.span()[0] - last_newline == caret_pos:
                    log.info("Hit hat a caret (^) on it")
                    text = drv_log[hit.span()[0] : hit.span()[1]][6:]
                    opts[text] = ""
        if opts:
            return opts

    @staticmethod
    def apply(opts):
        return RuleOutput(dep_constraints=opts)


class KernelHeaders(Rule):
    @staticmethod
    def match(drv, drv_log, opts, _rules_here):
        return "apt-get install linux-headers" in drv_log

    @staticmethod
    def apply(opts):
        return RuleOutput(
            arguments=["pkgs"],
            src_attrset_parts={
                "postPatch": """
                if [ -e setup.py ]; then
                     substituteInPlace setup.py --replace-quiet /usr/include ${pkgs.linuxHeaders}/include
                fi
                """,
                "nativeBuildInputs": [nix_literal("pkgs.linuxHeaders")],
            },
        )
