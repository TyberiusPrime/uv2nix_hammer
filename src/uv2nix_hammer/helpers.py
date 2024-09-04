import tarfile
import logging
import zipfile
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
    pkg = "-".join(parts[2:-1])
    pkg_tuple = (pkg, version)
    return pkg_tuple


def extract_pyproject_toml_from_archive(src_path):
    if src_path.endswith(".tar.gz"):
        tf = tarfile.open(src_path, "r:gz")
        candidates = []
        for fn in tf.getnames():
            if fn.endswith("pyproject.toml"):
                candidates.append(fn)
        candidates.sort(key = lambda x: len(x))
        if not candidates:
            raise ValueError("no pyproject.toml")
        with tf.extractfile(candidates[0]) as f:
            return toml.loads(f.read().decode("utf-8"))
    elif src_path.endswith(".zip"):
        with zipfile.ZipFile(src_path) as zf:
            try:
                with zf.open("pyproject.toml") as f:
                    return toml.loads(f.read().decode("utf-8"))
            except KeyError:
                raise ValueError("no pyproject.toml")
    else:
        raise ValueError("not an archive")


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

    def __init__(self, 
                 build_inputs = [],
                 arguments = [], src_attrset_parts = {},wheel_attrset_parts = {}):
        self.build_inputs = build_inputs
        self.arguments = arguments
        self.src_attrset_parts = src_attrset_parts
        self.wheel_attrset_parts = wheel_attrset_parts



class Rule: # marker class for rules
    pass
