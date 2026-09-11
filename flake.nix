{
  description = "Automatic, remembered and drag-and-drop display layouts for Hyprland, with a Noctalia panel";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = {
    self,
    nixpkgs,
  }: let
    systems = ["x86_64-linux" "aarch64-linux"];
    forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
  in {
    # The hypr-display command with no profiles baked in (every layout falls back
    # to preferred mode, automatic placement, scale 1). Use the Home Manager
    # module to build it with your profiles.
    packages = forAll (pkgs: rec {
      hypr-display = import ./nix/controller.nix {
        inherit pkgs;
        profiles = {};
      };
      default = hypr-display;
    });

    checks = forAll (pkgs: {
      tests =
        pkgs.runCommand "hypr-display-tests" {
          nativeBuildInputs = [pkgs.python3];
          src = ./controller;
        } ''
          cp -r $src controller
          cd controller
          python3 -m unittest test_displays
          touch $out
        '';
    });

    homeManagerModules = {
      # services.displayProfiles: controller, rollback service, Hyprland hooks, desktop entry.
      default = import ./nix/module.nix;
      displayProfiles = import ./nix/module.nix;
      # Registers ./plugin as a Noctalia plugin source and enables rdobre/displays.
      # Import it only when Noctalia's Home Manager module is also imported.
      noctalia = import ./nix/noctalia.nix;
    };
    homeModules = self.homeManagerModules;

    # For a hand-written Noctalia plugin source: kind = "path", location = "${hypr-display}/plugin".
    noctaliaPluginSource = ./plugin;
  };
}
