{
  inputs = {
    treefmt-nix.url = "github:numtide/treefmt-nix";
    nixpkgs.url = "github:/nixos/nixpkgs/master";
  };
  outputs = {
    systems,
    treefmt-nix,
    nixpkgs,
    ...
  }: let
    eachSystem = f: nixpkgs.lib.genAttrs (import systems) (system: f nixpkgs.legacyPackages.${system});
    treefmtEval = eachSystem (pkgs: treefmt-nix.lib.evalModule pkgs ./dev/treefmt.nix);
    inherit (nixpkgs) lib;
  in {
    formatter = eachSystem (pkgs: treefmtEval.${pkgs.system}.config.build.wrapper);
    devShell = eachSystem (pkgs:
      pkgs.mkShell {
        buildInputs = [pkgs.uv pkgs.rsync (pkgs.python312.withPackages (p: [p.rich p.packaging p.toml p.urllib3]))];
      });
  };
}
