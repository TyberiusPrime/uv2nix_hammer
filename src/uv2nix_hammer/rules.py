from .helpers import extract_pyproject_toml_from_archive, get_src


class BuildSystems:
    @staticmethod
    def normalize_build_system(bs):
        if ">" in bs:
            bs = bs[: bs.index(">")]
        if "<" in bs:
            bs = bs[: bs.index("<")]
        if ";" in bs:
            bs = bs[: bs.index(";")]
        bs = bs.replace("_", "-")
        return bs.lower()

    @classmethod
    def match(cls, drv, log, opts):
        if opts is None:  # no build system yet.
            opts = []
            try:
                pyproject_toml = extract_pyproject_toml_from_archive(get_src(drv))
                print(drv)
                print("\tgot pyproject.toml")
                opts = list(
                    set(
                        [
                            cls.normalize_build_system(x)
                            for x in pyproject_toml["build-system"]["requires"]
                        ]
                    )
                )
                print("\t", opts)
            except ValueError:
                opts = ["setuptools"]
        if "No module named 'setuptools'" in log:
            opts.append("setuptools")
        return opts

    @staticmethod
    def apply(opts):
        return (opts, [], "")


# class SetuptoolsSCM:
#     @staticmethod
#     def match(log):
#         return "setuptools_scm" in log

#     @staticmethod
#     def apply():
#         return (["setuptools"], [], "")


# class Mesonpy:
#     @staticmethod
#     def match(log):
#         return "ModuleNotFoundError: No module named 'mesonpy'" in log

#     @staticmethod
#     def apply():
#         return (["meson-python"], [], "")
