# uv2nix_hammer


Every nail needs a hammer.

uv2nix, by design, does not include overrides for python packages.

Therefore we have the [uv2nix_hammer_overrides](https://github.com/TyberiusPrime/uv2nix_hammer_overrides/) collection.

That collection is semi-automatically generated, for many of the necessary overrides can be found mechanically by applying a fixed rule set and trying to build.

This tool does so for arbitrary pypi packages.

Usage:
```
uv2nix_hammer <package-name> [version]
```

What it does:

uv2nix_hammer will create a folder 'hammer_build_<package-name>-version/build' with a nix uv2nix flake, 
a pyroject.toml requesting just that package (and a matching uv.lock), 
and attempt to build it using freshly cloned overrides from the
uv2nix_hammer_overrides repository. (in
'hammer_build_<package-name>-version/overrides').

If successfull, you'll end up with a PR ready branch in 'hammer_build_<package-name>-version/overrides' 
that you can use to contribute the overrides back to the [uv2nix_hammer_overrides repository](https://github.com/TyberiusPrime/uv2nix_hammer_overrides/)




---

