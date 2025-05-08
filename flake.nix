{
  inputs = {
    treefmt-nix.url = "github:numtide/treefmt-nix";
    nixpkgs.url = "github:/nixos/nixpkgs/master";
  };
  outputs = {
    self,
    systems,
    treefmt-nix,
    nixpkgs,
    ...
  }: let
    eachSystem = f: nixpkgs.lib.genAttrs (import systems) (system: f nixpkgs.legacyPackages.${system});
    # treefmtEval = eachSystem (pkgs: treefmt-nix.lib.evalModule pkgs ./dev/treefmt.nix);
    # inherit (nixpkgs) lib;
  in {
    # formatter = eachSystem (pkgs: treefmtEval.${pkgs.system}.config.build.wrapper);
    devShell = eachSystem (pkgs:
      pkgs.mkShell {
        buildInputs = [pkgs.uv pkgs.rsync (pkgs.python312.withPackages (p: [p.rich p.packaging p.toml p.urllib3]))];
      });
    packages = eachSystem (
      pkgs: {
        default = pkgs.python312Packages.buildPythonPackage {
          name = "uv2nix_hammer";
          format = "pyproject";
          src = ./.;
          nativeBuildInputs = [pkgs.python312Packages.hatchling];
          propagatedBuildInputs = with pkgs.python312Packages; [uv rich packaging toml urllib3 networkx];
        };
      }
    );
    apps = eachSystem (
      pkgs: {
        default = {
          type = "app";
          program = "${self.packages.${pkgs.system}.default}/bin/uv2nix-hammer";
        };
      }
    );
  };
}
