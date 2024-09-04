from .helpers import extract_pyproject_toml_from_archive, get_src, log, RuleOutput, Rule


class BuildSystems(Rule):
    @staticmethod
    def normalize_build_system(bs):
        for char in ">;<=":
            if char in bs:
                bs = bs[: bs.index(char)]
        bs = bs.replace("_", "-")
        return bs.lower()

    @classmethod
    def match(cls, drv, drv_log, opts):
        if opts is None:  # no build system yet.
            opts = []
            try:
                src = get_src(drv)
                try:
                    pyproject_toml = extract_pyproject_toml_from_archive(src)
                    log.debug(f"\tgot pyproject.toml for {drv}")
                    opts = sorted(
                        set(
                            [
                                cls.normalize_build_system(x)
                                for x in pyproject_toml["build-system"]["requires"]
                            ]
                        )
                    )
                    log.debug("\tfound build-systems: {opts}")
                except ValueError:
                    opts = ["setuptools"]
            except ValueError:
                opts = []  # was a wheel
        if "No module named 'setuptools'" in drv_log:
            if not "setuptools" in opts:
                opts.append("setuptools")
        if "RuntimeError: Running cythonize failed!" in drv_log and "cython" in opts:
            log.debug("detected failing cython - trying cython_0")
            opts.remove("cython")
            opts.append("cython_0")
            opts = sorted(opts)
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
