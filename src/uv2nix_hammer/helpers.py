import tarfile
import urllib3
import logging
import zipfile
import re
import toml
import json
import subprocess
from rich.logging import RichHandler

FORMAT = "%(message)s"
logging.basicConfig(
    level="NOTSET", format=FORMAT, datefmt="[%X]", handlers=[RichHandler()]
)

log = logging.getLogger("rich")
log.info("Hello, World!")


def drv_to_pkg_and_version(drv):
    nix_name = drv.split("/")[-1]
    parts = nix_name[:-4].rsplit("-")
    version = parts[-1]
    # if 'python' in parts[1]: # changed with pyproject.nix buidls
    #     pkg = "-".join(parts[2:-1])
    # else:
    pkg = "-".join(parts[1:-1])
    pkg_tuple = (pkg, version)
    return pkg_tuple


def extract_pyproject_toml_from_archive(src_path):
    return toml.loads(search_and_extract_from_archive(src_path, "pyproject.toml"))


def search_in_archive(src_path, filename):
    if src_path.endswith(".tar.gz"):
        tf = tarfile.open(src_path, "r:gz")
        candidates = []
        for fn in tf.getnames():
            if fn.endswith(filename):
                candidates.append(fn)
        candidates.sort(key=lambda x: len(x))
        if not candidates:
            raise KeyError(f"no {filename}")
        log.debug(f"Found {candidates[0]} for {filename}")
        return candidates[0]
    elif src_path.endswith(".zip"):
        # todo: should we not search in this as well?
        # if so, fix search_and_extract_from_archive as well
        with zipfile.ZipFile(src_path) as zf:
            try:
                with zf.open(filename) as f:
                    return filename
            except KeyError:
                raise KeyError(f"no {filename}")
    else:
        raise ValueError("not an archive")

def search_and_extract_from_archive(src_path, filename):
    """Read a file from archive and return it's contents.

    Searchs for the file in arbitrary sub folders,
    the one with the shortest overall name is used"""

    if src_path.endswith(".tar.gz"):
        tf = tarfile.open(src_path, "r:gz")
        candidates = []
        for fn in tf.getnames():
            if fn.endswith(filename):
                candidates.append(fn)
        candidates.sort(key=lambda x: len(x))
        if not candidates:
            raise KeyError(f"no {filename}")
        log.debug(f"Found {candidates[0]} for {filename}")
        with tf.extractfile(candidates[0]) as f:
            return f.read().decode("utf-8")
    elif src_path.endswith(".zip"):
        # todo: should we not seacrh in this as well?
        with zipfile.ZipFile(src_path) as zf:
            try:
                with zf.open(filename) as f:
                    return f.read().decode("utf-8")
            except KeyError:
                raise KeyError(f"no {filename}")
    else:
        raise ValueError("not an archive")


def has_pyproject_toml(drv):
    try:
        src = get_src(drv)
        extract_pyproject_toml_from_archive(src)
        return True
    except:
        return False

def get_pyproject_toml(drv):
    src = get_src(drv)
    try:
        return extract_pyproject_toml_from_archive(src)
    except:
        raise KeyError("no pyproject.toml")



def get_src(drv):
    derivation = json.loads(
        subprocess.check_output(
            ["nix", "derivation", "show", drv], text=True, stderr=subprocess.PIPE
        )
    )[drv]
    env = derivation["env"]
    src = env["src"]
    return src


class RuleOutput:
    def __init__(
        self,
        *,
        build_systems=None,
        arguments=[],
        src_attrset_parts={},
        wheel_attrset_parts={},
        requires_nixpkgs_master=False,
        dep_constraints=None,
        python_downgrade=None,
    ):
        self.build_systems = build_systems
        self.arguments = arguments
        self.src_attrset_parts = src_attrset_parts
        self.wheel_attrset_parts = wheel_attrset_parts
        self.requires_nixpkgs_master = requires_nixpkgs_master
        self.dep_constraints = dep_constraints
        self.python_downgrade = python_downgrade


class RuleFunctionOutput:
    def __init__(self, nix_func, args=[]):
        self.inner = nix_func
        self.args = args


class RuleOutputTriggerExclusion:
    def __init__(self, reason):
        self.reason = reason


class RuleOutputCopyFile:
    def __init__(self, files):
        self.files = files


class Rule:  # marker class for rules
    pass


def get_release_date(pkg, version):
    import datetime

    url = f"https://pypi.org/pypi/{pkg}/json"
    resp = urllib3.request("GET", url)
    json = resp.json()
    latest = datetime.datetime(2000, 1, 1)
    for file in json["releases"][version]:
        upload_time = datetime.datetime.fromisoformat(file["upload_time"])
        if upload_time > latest:
            latest = upload_time
    return latest


def normalize_python_package_name(pkg):
    return re.sub(r"[-_.]+", "-", pkg).lower()


def extract_source(src, target_folder):
    if src.endswith(".tar.gz"):
        with tarfile.open(src) as tf:
            tf.extractall(target_folder)
    elif src.endswith(".zip"):
        with zipfile.ZipFile(src) as zf:
            zf.extractall(target_folder)
    elif src.endswith(".whl"):
        pass
    else:
        log.warn(f"Unknown archive type, not unpacked {src}")
