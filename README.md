# uv2nix_hammer


Every nail needs a hammer.

uv2nix, by design, does not include overrides for python packages.

Much of the necessary overrides can be found mechanically though by applying 
a fixed rule set and trying to build.

This tool does so for arbitrary python packages.

Usage:
```
uv2nix_hammer <package-name> [version]
```

It will create a folder 'hammer_build_<package-name>-version/build' with a nix uv2nix flake,
a pyroject.toml requesting just that package (a uv.lock), and attempt to build it using
freshly cloned overrides from the uv2nix_hammer_overrides repository. (in 'hammer_build_<package-name>-version/overrides').

If successfull, you'll end up with a PR ready branch in 'hammer_build_<package-name>-version/ovverides' 
that you can use to contribute the overrides back to the uv2nix_hammer_overrides repository.
