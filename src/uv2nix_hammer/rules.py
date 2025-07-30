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
    get_pyproject_toml,
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
                try:
                    src = get_src(drv)
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
        if (
            "No module named 'setuptools'" in drv_log
            or "Cannot import 'setuptools.build_meta'" in drv_log
        ):
            if not "setuptools" in opts:
                opts.append("setuptools")
        if "No module named pip" in drv_log and not "pip" in opts:
            opts.append("pip")
        if "RuntimeError: Running cythonize failed!" in drv_log and "cython" in opts:
            log.debug("detected failing cython - trying cython_0")
            opts.remove("cython")
            opts.append("cython_0")
        if "Missing dependencies:" in drv_log:
            from_here = drv_log[drv_log.find("Missing dependencies:") :]
            lines = [x.strip() for x in from_here.split("\n")]
            # log.error(f"Missing dependencies - {drv}")
            if "setuptools-scm" in lines or "setuptools_scm" in lines:
                opts.append("setuptools-scm")
            if "setuptools-git" in lines or "setuptools_git" in lines:
                opts.append("setuptools-git")
            # if "setuptools-git-version" in drv_log:
            #     opts.append("setuptools-git-version") # currently not in nixpkgs
            if "pytest-runner" in from_here:
                opts.append("pytest-runner")

            if "pycodestyle" in lines:
                opts.append("pycodestyle")
            if "isort" in lines:
                opts.append("isort")
            if "Cython<3,>=0.29.22" in from_here or "cython<=3" in from_here:
                opts.append("cython_0")
            elif "cython>=3" in from_here:
                opts.append("cython")
            elif (
                "cython" in lines
                or "Cython" in lines
                or "Cython>=" in from_here
                or "cython>=" in from_here
            ):
                opts.append("cython")
            if "pip" in lines:
                opts.append("pip")
            if "pbr" in lines:
                opts.append("pbr")
            if "cffi" in from_here:
                opts.append("cffi")
            if (
                "numpy" in lines
                or "numpy;" in from_here
                or "numpy>" in from_here
                or "numpy=" in from_here
            ):
                opts.append("numpy")
            if "wheel" in lines:
                opts.append("wheel")
            if "torch" in lines:
                opts.append("torch")
            if "ninja" in lines:
                opts.append("ninja")
            if "requests" in lines:
                opts.append("requests")
            if "pbr>" in from_here:
                opts.append("pbr")
            if "certifi>" in from_here:
                opts.append("certifi")
            if "versiontools>" in from_here:
                opts.append("versiontools")
            if "fastrlock" in from_here:
                opts.append("fastrlock")
            if "vcversioner" in from_here:
                opts.append("vcversioner")
            if "flake8" in lines:
                opts.append("flake8")
            if "versioneer" in lines:
                opts.append("versioneer")
            if "pytest-benchmark" in lines:
                opts.append("pytest-benchmark")
            if "sphinx" in lines:
                opts.append("sphinx")
        if "cppyy-cling" in drv_log:
            opts.append("cppyy-cling")
        if "cppyy-backend" in drv_log:
            opts.append("cppyy-backend")
        if (
            "ModuleNotFoundError: No module named 'numpy'" in drv_log
            or "install requires: 'numpy'" in drv_log
            or "pip install numpy" in drv_log
        ):
            opts.append("numpy")
        if "ModuleNotFoundError: No module named 'pandas'" in drv_log:
            opts.append("pandas")
        if "ModuleNotFoundError: No module named 'convertdate'" in drv_log:
            opts.append("convertdate")
        if "ModuleNotFoundError: No module named 'lunarcalendar'" in drv_log:
            opts.append("lunarcalendar")
        if "ModuleNotFoundError: No module named 'holidays'" in drv_log:
            opts.append("holidays")
        if "ModuleNotFoundError: No module named 'toml'" in drv_log:
            opts.append("toml")
        if "ModuleNotFoundError: No module named 'cffi'" in drv_log:
            opts.append("cffi")
        if "ModuleNotFoundError: No module named 'pygments'" in drv_log:
            opts.append("pygments")
        if "No module named 'pybind11'" in drv_log:
            if not "pybind11" in opts:
                opts.append("pybind11")

        if "ModuleNotFoundError: No module named 'fil3s'" in drv_log:
            opts.append("fil3s")
        if "No matching distribution found for matplotlib" in drv_log:
            opts.append("matplotlib")
        if (
            "ModuleNotFoundError: No module named 'Cython'" in drv_log
        ):  # if you're so old that you don't have a pyproject.toml, but non managed build requirements, you probably also want the old cython,
            opts.append("cython")
        opts = [x for x in opts if x != pkg]

        if not "cython" in opts and not "cython_0" in opts:
            if (
                "Cython.Compiler.Errors.CompileError:" in drv_log
                or " No matching distribution found for cython" in drv_log
            ):
                opts.append("cython")
            elif (
                "error: ‘PyThreadState’ {aka ‘struct _ts’} has no member named ‘exc_traceback’; did you mean ‘curexc_traceback’?"
                in drv_log
            ):
                opts.append("cython")

        opts = sorted(set(opts))
        if "poetry" in opts:
            opts.remove("poetry")
            opts.append("poetry-core")

        while "cython" in opts and "cython_0" in opts:
            opts.remove("cython")
        if (
            "could not find git for clone of pybind11-populate" in drv_log
            or "pybind11Config.cmake" in drv_log
        ):
            opts.append("pybind11")
        if "No such file or directory: 'cmake'" in drv_log:
            opts.append("cmake")
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
        return (
            "ModuleNotFoundError: No module named 'poetry.masonry'" in drv_log
            or "BackendUnavailable: Cannot import 'poetry.masonry.api'" in drv_log
        )

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
        if "Missing dependencies:" in drv_log:
            try:
                pyproject_toml = get_pyproject_toml(
                    drv, forbidden_paths=["third_party"]
                )
                if "build-system" in pyproject_toml and (
                    "requires" in pyproject_toml
                    or "requires" in pyproject_toml["build-system"]
                ):
                    start = drv_log[drv_log.find("Missing dependencies:") :]
                    next_line = start[start.find("\n") + 1 :]
                    next_line = next_line[: next_line.find("\n")]
                    return [x in next_line for x in requirements_sep_chars]
            except KeyError:
                pass

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
            if x.startswith("~literal:!"):
                return x[11:]
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
            (
                "configure: error: No fortran compiler found, please set the FC flag",
                "gfortran",
            ),
            ("but no Fortran compiler found", "gfortran"),
            ("No CMAKE_Fortran_COMPILER could be found.", "gfortran"),
            ("Did not find pkg-config", "pkg-config"),
            ("pkgconfig", "pkg-config"),
            ("pkg-config not found", "pkg-config"),
            ("'pkg-config' is required", "pkg-config"),
            ("pkg-config: not found", "pkg-config"),
            ("cannot execute pkg-config", "pkg-config"),
            ("Install pkg-config.", "pkg-config"),
            ("missing: PKG_CONFIG_EXECUTABLE", "pkg-config"),
            ("No such file or directory: 'pkg-config'", "pkg-config"),
            (
                "The headers or library files could not be found for zlib",
                ["zlib.dev", "pkg-config"],
            ),
            ("zlib.h: No such file or directory", ["zlib.dev", "pkg-config"]),
            ("pkg-config is required for building", "pkg-config"),
            ('"pkg-config" command could not be found.', "pkg-config"),
            ("CMake must be installed to build from source.", "cmake"),
            ("Did not find CMake 'cmake'", "cmake"),
            ("need to install CMake", "cmake"),
            ("Failed to install temporary CMake", "cmake"),
            ("CMake is not installed on your system!", "cmake"),
            ("Missing CMake executable", "cmake"),
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
            ("bzlib.h: No such file or directory", ["bzip2.dev", "pkg-config"]),
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
            ("which: not found", "which"),
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
            ("glib.h: No such file", ["pkgs.glib.dev", "pkg-config"]),
            ("jpeglib.h: No such file", "libjpeg"),
            ("png.h: No such file", "libpng"),
            ("tiffio.h: No such file", "libtiff"),
            ("unrar/dll.hpp: No such", "unrar"),
            ("iwlib.h: No such file", "wirelesstools"),
            ("command 'swig' failed: No such file or directory", "swig"),
            (
                "Boost Python3 library not found",
                nix_literal(
                    "(pkgs.boost.override {python = final.python; numpy=final.numpy; enablePython=true;})"
                ),
            ),
            ("Could NOT find GLIB2", "glib"),
            ("No package 'gfal2' found", "gfal2"),
            ("Package 'libpcre2-8', required by 'glib-2.0', not found", "pcre2"),
            ("mpfr.h: No such file", "mpfr"),
            ("fplll/fplll_config.h: No such file", "fplll_20160331"),
            ("OSError: mariadb_config not found.", "libmysqlclient"),
            ("mpi.h: No such file", "mpi"),
            ("autoreconf: not found", "autoconf"),
            ("No such file or directory: 'autoreconf'", "autoconf"),
            ('Can\'t exec "libtoolize"', "libtool"),
            ('Can\'t exec "aclocal"', "automake"),
            ("Libtool library used but 'LIBTOOL'", "libtool"),
            ("glpk.h: No such file", "glpk"),
            ("could not start gsl-config", "gsl.dev"),
            ("openssl/ssl.h: No such file", "openssl"),
            (" openssl/aes.h: No such file", "openssl"),
            (
                "sqlite3.h: No such file",
                "sqlite",
            ),
            ("winscard.h: No such file", "pcsclite"),
            ("hunspell.hxx: No such file", "hunspell.dev"),
            ("re2/re2.h: No such file", "re2"),
            ("Eigen/Core: No such file", "eigen"),
            ("No package 'ddjvuapi' found", "djvulibre"),
            ("libmilter/mfapi.h: No such file", "libmilter"),
            ("Error: pg_config executable not found.", "postgresql.dev"),
            ("cudaProfiler.h: No such", "cudaPackages.cuda_profiler_api"),
            ("curand.h: No such file", "cudaPackages.libcurand"),
            ("crt/host_config.h: No such file", "cudaPackages.cuda_nvcc"),
            ("exiv2/exiv2.hpp: No such fil", "exiv2"),
            (
                "boost/python.hpp: No such file",
                "(pkgs.boost.override {python = final.python; numpy=final.numpy; enablePython=true;})",
            ),
            ("libxml/xmlreader.h: No such file or directory", "libxml2.dev"),
            ("Could NOT find ZLIB", "pkgs.zlib.dev"),
            # (" Unable to find the blosc2 library.", "c-blosc2"),
            # ("libxml/xpath.h: No such file or directory", "libxml2"),
            # (
            #     "-lldap_r: No such file",
            #     ["pkgs.openldap.dev", "pkg-config", "cyrus_sasl"],
            # ),
            # ("Installing this module requires OpenSSL python bindings", "final.pyopenssl"),
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
        args = [x for x in args if x[0] != "("]
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
            ("libcusparseLt.so.0 -> not found!", "cudaPackages.cusparselt"),
            ("libcufile.so.0 -> not found!", "cudaPackages.libcufile"),
            ("libcusolver.so.11 -> not found!", "cudaPackages.libcusolver"),
            (
                "libcudart.so.12 -> not found",
                nix_literal(
                    '] ++ (pkgs.lib.optionals ((builtins.trace pkgs.stdenv.hostPlatform.system pkgs.stdenv.hostPlatform.system) == "x86_64-linux") [ pkgs.cudaPackages.cuda_cudart ]) ++ ['  # what an ugly hack ^^
                ),
            ),
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
            ("libboost_chrono.so.1.83.0 -> not found!", "boost183"),
            ("libboost_filesystem.so.1.83.0 -> not found!", "boost183"),
            ("libboost_python312.so.1.83.0 -> not found!", "boost183"),
            ("libboost_serialization.so.1.83.0 -> not found!", "boost183"),
            ("libboost_system.so.1.83.0 -> not found!", "boost183"),
            (
                "libboost_python312.so.1.83.0 -> not found!",
                nix_literal("""
             (pkgs.boost183.override {
                 python = final.python;
                 numpy = final.numpy;
                 enablePython = true;
             })
             """),
            ),
            ("Boost library location was not found!", ["boost", "pkg-config"]),
            ("libconsole_bridge.so.1.0 -> not found!", "console-bridge"),
            ("libeigenpy.so -> not found!", "final.eigenpy"),
            ("libhpp-fcl.so -> not found!", "hpp-fcl"),
            ("liboctomap.so -> not found!", "octomap"),
            ("liboctomath.so -> not found!", "octomap"),
            ("libtinyxml.so -> not found!", "tinyxml"),
            ("liburdfdom_model.so.3.0 -> not found!", "urdfdom"),
            ("liburdfdom_sensor.so.3.0 -> not found!", "urdfdom"),
            ("liburdfdom_world.so.3.0 -> not found!", "urdfdom"),
            ("libassimp.so.5 -> not found!", "assimp"),
            ("libqhull_r.so.8.0 -> not found!", "qhull"),
            ("libudev.so.1 -> not found!", "udev"),
            ("glib.h: No such file", "glib"),
            ("crack.h: No such file", "cracklib"),
            ("libjvm.so", "openjdk"),
            ("udunits2.h: No such file", "udunits"),
            ("libprecice not found", "precice"),
            (
                " cups/http.h: No such file",
                "cups",
            ),  # libiconv on darwin, but needs extension here.
            ("libOpenCL.so.1 -> not found!", "ocl-icd"),
            ("libze_loader.so.1 -> not found!", "level-zero"),
            ("Could NOT find OpenSSL", "openssl"),
            ("graphviz/cgraph.h: No such file", "graphviz"),
            ("-lz: No such file", "zlib"),
            ("libssl.so.1.1 -> not found!", "openssl_1_1"),
            ("libcrypto.so.1.1 -> not found!", "openssl_1_1"),
            ("libz.so.1 -> not found!", "zlib"),
            ("libkeyutils.so.1", "keyutils"),
            ("sasl/sasl.h: No such file", "cyrus_sasl"),
            ("Installing gifsicle on Linux requires sudo!", "gifsicle"),
            ("could not start gsl-config", "gsl"),
            ("libhwloc.so.15 -> not found!", "hwloc"),
            # (" RequiredDependencyException: pangocairo", "pango"),
            ("libexiv2.so.28 -> not found!", "exiv2"),
            ("libpcsclite.so.1 -> not found", "pcsclite"),
            ("libcurl.so.4 -> not found!", "curl"),
            ("libssl.so.3 -> not found!", "openssl"),
            (
                "libgfortran.so.5 -> not found!",
                [
                    nix_literal("pkgs.gfortran13.cc"),
                    nix_literal("pkgs.gfortran13.out"),
                ],
            ),
            ("cannot find -lhunspell", "pkgs.hunspell.out"),
            ("Failed to find Gammu!", "gammu"),
            ("libXcursor.so.1 -> not found!", "pkgs.xorg.libXcursor"),
            ("libXfixes.so.3 -> not found!", "pkgs.xorg.libXfixes"),
            ("libXft.so.2 -> not found!", "pkgs.xorg.libXft"),
            ("libfontconfig.so.1 -> not found!", "pkgs.fontconfig"),
            ("libXinerama.so.1 -> not found!", "pkgs.xorg.libXinerama"),
            ("libkrb5.so.3 -> not found!", "krb5"),
            ("Could NOT find BLAS", ["blas", "lapack"]),
            ("openblas", ["openblas", "pkg-config"]),
            ("umfpack", ["suitesparse"]),
            ("pull submodule rabbitmq-c.", "rabbitmq-c"),
            ("libdbus-1.so.3 -> not found!", "dbus"),
            ("libusb-1.0.so.0 -> not found!", "libusb1"),
            ("libbluetooth.so.3 -> not found!", "bluez"),
            ("libgtk-x11-2.0.so.0", ["gtk2", "pkg-config"]),
            ("libcairo.so.2 -> not found!", "cairo"),
            ("libpango-1.0.so.0 -> not found!", "pango"),
            ("mysql_config not found", "libmysqlclient"),
            ("libnl-3.so.200 -> not found!", "libnl"),
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
        arguments = {"pkgs"}
        fixups = ""
        for pkg in opts:
            if pkg.startswith("~literal:!:final."):
                pkg_str = pkg[len("~literal:!:final.") :]
                cmeel_packages = ["eigenpy"]
                if pkg_str in cmeel_packages:
                    # no clue why the place the .so files there.
                    fixups += f"addAutoPatchelfSearchPath ${{final.{pkg_str}}}/${{final.python.sitePackages}}/cmeel.prefix/${{final.python.sitePackages}}\n"
                else:
                    fixups += f"addAutoPatchelfSearchPath ${{final.{pkg_str}}}/${{final.python.sitePackages}}/{pkg_str}/lib\n"
                arguments.add("final")
            if pkg == "~literal:!:pkgs.openjdk":
                pkg_str = pkg[len("~literal:!:") :]
                fixups += f"addAutoPatchelfSearchPath ${{{pkg_str}}}/lib/openjdk/lib/server/\n"
            if "final." in pkg:
                arguments.add("final")
        src_attr_parts = {"buildInputs": opts, "env": env}
        wheel_attr_parts = {"buildInputs": opts}
        if fixups:
            wheel_attr_parts["preFixup"] = fixups
            # src_attr_parts["libs"] = libs
        return RuleOutput(
            arguments=sorted(arguments),
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


class MasterForTorch(Rule):  # torch requires nixpkgs master.
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


# class RemovePropagatedBuildInputs(Rule):
#     @staticmethod
#     def match(drv, drv_log, opts, _rules_here):
#         pass

#     @staticmethod
#     def apply(opts):
#         return RuleOutput(
#             arguments=["final", "helpers", "pkgs"],
#             src_attrset_parts={
#                 "propagatedBuildInputs": nix_literal(f"""
#                   (helpers.removePackagesByName
#                     (old.propagatedBuildInputs or [ ])
#                     (pkgs.lib.optionals (final ? {opts}) [ final.{opts} ]))
#             """)
#             },
#             wheel_attrset_parts={
#                 "propagatedBuildInputs": nix_literal(f"""
#                   (helpers.removePackagesByName
#                     (old.propagatedBuildInputs or [ ])
#                     (pkgs.lib.optionals (final ? {opts}) [ final.{opts} ]))
#             """)
#             },
#         )


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
            arguments=["pkgs"],
            src_attrset_parts={
                "preBuild": """
                cd /build
                # find the first directory containing either pyproject.toml or setup.py
                buildDir=$(find . -maxdepth 1 -type d -exec test -e "{}/pyproject.toml" -o -e "{}/setup.py" \\; -print -quit)
                cd $buildDir
                """
            },  # will fail if there's multiple directories
        )


class Torch(Rule):
    @staticmethod
    def match(drv, drv_log, opts, _rules_here):
        return (
            "libc10_cuda.so -> not found!" in drv_log
            or "libtorch.so -> not found!" in drv_log
        )

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


class DowngradeSetupTools(Rule):
    @staticmethod
    def match(drv, drv_log, opts, _rules_here):
        if (
            "TypeError: canonicalize_version() got an unexpected keyword argument 'strip_trailing_zero'"
            in drv_log
        ):
            return "<71"

    @staticmethod
    def apply(opts):
        return RuleOutput(dep_constraints={"setuptools": opts})


class DowngradePytestRunner(Rule):
    @staticmethod
    def match(drv, drv_log, opts, _rules_here):
        if "pytest-runner<5.0" in drv_log:
            return "<5.0"

    @staticmethod
    def apply(opts):
        return RuleOutput(dep_constraints={"pytest-runner": opts})


class DowngradeNumpy(Rule):
    """Downgrade numpy when it's a clear >= 2.0 not suppported case"""

    @staticmethod
    def match(drv, drv_log, opts, _rules_here):
        if "'int_t' is not a type identifier" in drv_log and "np.int_t" in drv_log:
            return "<2"
        elif "No module named 'numpy.distutils'" in drv_log:
            return "<1.22"
        elif " double I = intensity(" in drv_log:
            return "<1.22"
        elif " numpy/arrayobject.h: No such file" in drv_log:
            return "<1.22"
        elif (
            "struct _PyArray_Descr" in drv_log
            and "has no member named" in drv_log
            and "subarray" in drv_log
        ):
            return "<2"  # https://github.com/piskvorky/gensim/issues/3541
        elif (
            'origin = find_spec("numpy").origin' in drv_log
            and "AttributeError: 'NoneType' object has no attribute 'origin" in drv_log
        ):
            return "<2"
        elif (
            "error: request for member ‘imag’ in something not a structure or union"
            in drv_log
        ):
            return "<2"
        elif (
            "_PyArray_Descr" in drv_log
            and " has no member named" in drv_log
            and "names" in drv_log
        ):
            return "<2"

        # or ("numpy/arrayobject.h: No such file" in drv_log)

    @staticmethod
    def apply(opts):
        if opts is True:
            opts = "<2"
        return RuleOutput(dep_constraints={"numpy": opts})


class DowngradePython(Rule):
    """Downgrade python if necessary"""

    # always_reapply = True  # otherwise we don't apply it if we already had the rule.

    @staticmethod
    def match(drv, drv_log, opts, _rules_here):
        if "3.12" in drv_log and "No module named 'distutils'" in drv_log:
            log.error("Downgrading Python - distutils")
            return "3.10"
        if "greenlet-1.1.0" in drv_log:
            log.error("Downgrading Python - greenlet")
            return "3.10"
        if (
            "return kh_float64_hash_func(val.real)^kh_float64_hash_func(val.imag);"
            in drv_log
        ):
            log.error("Downgrading Python - old pandas")
            return "3.10"  # old pandas 1.5.3
        if "ModuleNotFoundError: No module named 'distutils'" in drv_log:
            return "3.11"
        if "fatal error: longintrepr.h: " in drv_log:
            log.error("Downgrading Python - longinterpr")
            return "3.10"
        if "AttributeError: fcompiler. Did you mean: 'compiler'?" in drv_log:
            # that's trying to compile numpy 1.22, o
            log.error("Downgrading Python - fcompiler")
            return "3.10"
        if "ModuleNotFoundError: No module named 'imp'" in drv_log:
            return "3.11"
        if "only versions >=3.6,<3.10 are supported." in drv_log:
            return "3.9"
        if "Cannot install on Python version 3.10." in drv_log:
            return "3.9"
        if (
            "Cannot install on Python version " in drv_log
            and "only versions >=3.8,<3.12" in drv_log
        ):
            return "3.11"
        if "pygame" in drv:
            pkg_tuple = drv_to_pkg_and_version(drv)
            version = pkg_tuple[1]
            if Version(version) <= Version("2.5.2"):
                return "3.11"
        if "cannot import name 'build_py_2to3' from 'distutils" in drv_log:
            return "3.9"
        if "ModuleNotFoundError: No module named 'distutils.msvccompiler'" in drv_log:
            return "3.9"  # old scipy
        if "requires python >= 3.6 and <=3.10" in drv_log:
            return "3.9"
        if "eval.h: No such file" in drv_log:
            log.error("Downgrading Python - eval.h")
            return "3.10"
        if "_PyUnicode_get_wstr_length(PyObject *op)" in drv_log:
            log.error("Downgrading Python - _PyUnicode_get_wstr_length")
            return "3.9"
        if "PyArray_Descr’} has no member named ‘subarray’" in drv_log:
            log.error("Downgrading Python - PyArray_Descr")
            return "3.10"
        if (
            "invalid literal for int() with base 10:" in drv_log
            and "in python_version" in drv_log
        ):  # pmisc - but get's excluded by 'max python = 3.8' anyway.
            return "3.9"
        if "ModuleNotFoundError: No module named 'symbol'" in drv_log:
            return "3.9"
        if (
            "‘PyLongObject’ {aka ‘struct _longobject’} has no member named ‘ob_digit’"
            in drv_log
        ):
            log.error("Downgrading Python - ob_digit")
            return "3.11"

    @staticmethod
    def apply(opts):
        # log.debug(f"Downgrading to python {opts}")
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
        if "print '" in drv_log or 'print "' in drv_log:
            return f"Is_python2_only (print '): {drv_to_pkg_and_version(drv)}"
        if re.search("except [^,]+,[^:]+:", drv_log):
            return f"Is_python2_only (except x, y:): {drv_to_pkg_and_version(drv)}"
        # todo: this needs a regexp
        if (
            "raise exc, " in drv_log
            or "raise my_exception, " in drv_log
            or "raise newexc, None, sys.exc_info()" in drv_log
        ):
            return f"Is_python2_only (raise exec,): {drv_to_pkg_and_version(drv)}"
        if "cannot import name 'quote' from 'urllib'" in drv_log:
            return f"Is_python2_only (looks for urllib.quote): {drv_to_pkg_and_version(drv)}"
        if "SyntaxError: invalid hexadecimal literal" in drv_log and "0xFFFFFFFAL":
            return f"Is_python2_only (long int, 0xFFFFFFFAL): {drv_to_pkg_and_version(drv)}"

    @staticmethod
    def apply(opts):
        return RuleOutputTriggerExclusion(opts)


class PyPIStub(Rule):
    @staticmethod
    def match(drv, drv_log, opts, _rules_here):
        if "wrong pypi" in drv_log:
            pkg_tuple = drv_to_pkg_and_version(drv)
            return f"Not actually on pypi/stub only: {pkg_tuple}"

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
        try:
            opts, extract_result = opts
        except:
            extract_result = False
        needed_patch = extract_result
        # todo: discern maturin & setuptoolsRust
        if needed_patch:
            return RuleFunctionOutput("""
              pkgs.lib.optionalAttrs (!helpers.isWheel old) (
              helpers.standardMaturin {
              furtherArgs = {
                  postPatch = ''
                  cp ${./Cargo.lock} Cargo.lock
                  '';
              };
              } old)
                                  """)

        else:
            return RuleFunctionOutput("""
                                  pkgs.lib.optionalAttrs (!helpers.isWheel old) (helpers.standardMaturin {} old)
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
        p = subprocess.Popen(
            [
                "nix",
                "shell",
                "github:/nixos/nixpkgs/master#cargo",
                "-c",
                "cargo",
                "check",
            ],
            cwd=cargo_toml.parent,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = p.communicate()
        if p.returncode != 0:
            raise ValueError(f"cargo check failed: {stderr.decode('utf-8')}")
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


class Enum34(Rule):  # a older variant of PythonTooNew
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
            return (
                f"setuptools too new, setup command: use_2to3 is invalid. {pkg_tuple}"
            )
        if "AttributeError: module 'platform' has no attribute 'dist'" in drv_log:
            pkg_tuple = drv_to_pkg_and_version(drv)
            return "AttributeError: module 'platform' has no attribute 'dist'"

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
            # I am looking for final.something where in the next line there's a ^ pointing at it.
            for hit in re.finditer("final\\.[a-z0-9A-Z-]+", drv_log):
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


class Udunits(Rule):
    @staticmethod
    def match(drv, drv_log, opts, _rules_here):
        return "Require to set UDUNITS2_XML_PATH" in drv_log

    @staticmethod
    def apply(opts):
        return RuleOutput(
            arguments=["pkgs"],
            src_attrset_parts={
                "env": {
                    "UDUNITS2_XML_PATH": "${pkgs.udunits}/share/udunits/udunits2.xml"
                },
            },
        )


class HomlessShelter(Rule):
    @staticmethod
    def match(drv, drv_log, opts, _rules_here):
        return "Permission denied: '/homeless-shelter'" in drv_log

    @staticmethod
    def apply(opts):
        return RuleOutput(
            src_attrset_parts={
                "env": {"HOME": "/tmp"},
            },
        )


class NvidiaCollision(Rule):
    @staticmethod
    def match(drv, drv_log, opts, _rules_here):
        pkg, version = drv_to_pkg_and_version(drv)
        return pkg.startswith("nvidia-cu")
        # leads to
        # Target: /nix/store/dlmjc4l64spkfy4dg4sj7zli0k2rdix7-test-venv/lib/python3.12/site-packages/nvidia/__pycache__/__init__.cpython-312.opt-2.pyc

    # >      File 1: /nix/store/rs7214axar5xx0v2xl1y0axic8k4mm3n-nvidia-nccl-cu12-2.20.5/lib/python3.12/site-packages/nvidia/__pycache__/__init__.cpython-312.opt-2.pyc
    # >      File 2: /nix/store/xpjpqyb5n4l6gaqxys6cgi4fdisflcf3-nvidia-cusparse-cu12-12.1.0.106/lib/python3.12/site-packages/nvidia/__pycache__/__init__.cpython-312.opt-2.pyc.
    # in a lot of nvidia-cu* packages other wise
    # and since that is only happening when the venv is being build
    # it's hard to backtraco to the offending derivation.
    # so we just name match here.
    # if re.search("Target: /nix/store/[^/]+/lib/python[^/]+/site-packages/nvidia/__pycache__/__init__.cpython-[^.]+\\.opt-2\\.pyc", drv_log):
    #     lines = drv_log.split("\n")
    #     pkg1 = extract_from_line([x for x in lines if 'File 1:' in x][0])
    #     pkg2 = extract_from_line([x for x in lines if 'File 2:' in x][0])
    #     return (pkg1, pkg2)

    @staticmethod
    def apply(opts):
        return RuleOutput(
            src_attrset_parts={
                "env": {"dontUsePyprojectBytecode": True},
            },
        )


class HD5DIR(Rule):
    @staticmethod
    def match(drv, drv_log, opts, _rules_here):
        return (
            "You may need to explicitly state where your local HDF5 headers" in drv_log
        )

    @staticmethod
    def apply(opts):
        return RuleOutput(
            arguments=["pkgs"],
            src_attrset_parts={
                "env": {
                    "HDF5_DIR": nix_literal("pkgs.lib.getDev pkgs.hdf5"),
                },
                "nativeBuildInputs": [
                    nix_literal("pkgs.pkg-config"),
                    nix_literal("pkgs.hdf5"),
                    nix_literal("final.blosc2"),
                ],
                "buildInputs": [nix_literal("pkgs.c-blosc2")],
            },
        )


class UnpackerNoDirectories(Rule):
    @staticmethod
    def match(drv, drv_log, opts, _rules_here):
        return "unpacker appears to have produced no directories" in drv_log

    @staticmethod
    def apply(opts):
        return RuleOutput(
            src_attrset_parts={
                "unpackPhase": nix_literal("""''
                mkdir src/${old.pname}/${old.version} -p
                cd src/${old.pname}/${old.version} 
                unzip $src
                ''""")
            },
        )
