import re
def nix_identifier(identifier):
    if re.match("^[A-Za-z_][A-Za-z0-9-]*$", identifier):
        return identifier
    else:
        return nix_format(identifier)  # format as string


def nix_path(path):
    return ("path", path)


def nix_literal(path):
    return ("literal", path)


def nix_format(value):
    if isinstance(value, str):
        return '"' + value.replace('"', '\\"') + '"'
    elif isinstance(value, (int, float)):
        return str(value)
    elif isinstance(value, tuple) and value[0] == "path":
        return "./" + str(value[1])
    elif isinstance(value, tuple) and value[0] == "literal":
        return str(value[1])
    elif isinstance(value, list):
        return "[" + " ".join((nix_format(x) for x in value)) + "]"
    else:
        res = "{"
        for k, v in sorted(value.items()):
            res += f"{nix_identifier(k)} = {nix_format(v)};"
        res += "}"
        return res


def main():
    nested = {}

    for default_nix in Path("overrides").rglob("default.nix"):
        ver = default_nix.parent.name
        pkg = default_nix.parent.parent.name
        if not pkg in nested:
            nested[pkg] = {}
        nested[pkg][ver] = nix_literal("import ./" + str(default_nix.parent))

    Path("collected.nix").write_text(nix_format(nested))
    subprocess.check_call(["nix", "fmt"])
