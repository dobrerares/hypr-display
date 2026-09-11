{
  config,
  lib,
  pkgs,
  ...
}: let
  cfg = config.services.displayProfiles;
  controller = import ./controller.nix {
    inherit pkgs;
    inherit (cfg) profiles noctaliaPackage hyprlandPackage;
  };
in {
  options.services.displayProfiles = {
    enable = lib.mkEnableOption "automatic, remembered and temporary Hyprland display layouts (hypr-display)";

    profiles = lib.mkOption {
      type = lib.types.attrsOf lib.types.attrs;
      default = {};
      description = ''
        Named automatic profiles. Each profile has `conditions` (which displays
        must be connected, optionally the lid state) and `monitors` (settings per
        connector name or `desc:<description>`). The most specific matching
        profile is applied whenever the set of connected displays or the lid
        state changes. See the README for the full schema.
      '';
      example = lib.literalExpression ''
        {
          docked = {
            conditions.requiredMonitors = [
              { description = "Samsung Display Corp. ATNA40CU05-0"; }
              { description = "Dell Inc. AW3423DWF BTW82S3"; }
            ];
            monitors = {
              "eDP-1" = { resolution = "2880x1800"; refreshRate = 120; position = "0x0"; scale = 1.33; vrr = true; };
              "desc:Dell Inc. AW3423DWF BTW82S3" = { resolution = "3440x1440"; refreshRate = 60; position = "2165x0"; scale = 1.0; };
            };
          };
        }
      '';
    };

    noctaliaPackage = lib.mkOption {
      type = lib.types.nullOr lib.types.package;
      default = null;
      description = ''
        Noctalia package used by the Present bundle (`noctalia msg caffeine-*`,
        `notification-dnd-*`). Set it to `config.programs.noctalia.package`.
        When null, `noctalia` is used from PATH if present; otherwise those two
        steps are skipped with a message and audio routing still works.
      '';
    };

    hyprlandPackage = lib.mkOption {
      type = lib.types.package;
      default = pkgs.hyprland;
      defaultText = lib.literalExpression "pkgs.hyprland";
      description = "Package providing `hyprctl`; keep it the same version as the running compositor.";
    };

    package = lib.mkOption {
      type = lib.types.package;
      default = controller;
      defaultText = lib.literalExpression "hypr-display built from `profiles`";
      description = "The hypr-display command installed and run by the service.";
    };

    desktopEntry = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Install a `Displays & Presenting` desktop entry that opens the Noctalia panel.";
    };

    hyprland.enable = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Add a catch-all `monitor` rule (every output starts enabled at its
        preferred mode) and a `config.reloaded` hook that re-applies the kept
        layout to the Home Manager Hyprland Lua configuration. Requires
        `wayland.windowManager.hyprland.configType = "lua"`.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    home.packages = [cfg.package];

    xdg.desktopEntries.displays = lib.mkIf cfg.desktopEntry {
      name = "Displays & Presenting";
      genericName = "Display Settings";
      exec = "noctalia msg panel-toggle rdobre/displays:panel";
      icon = "video-display";
      terminal = false;
      categories = ["Settings" "HardwareSettings"];
    };

    # Always start with an enabled output. The controller applies known profiles
    # after IPC is available; it never writes compositor configuration files.
    wayland.windowManager.hyprland.settings.monitor = lib.mkIf cfg.hyprland.enable [
      {
        output = "";
        mode = "preferred";
        position = "auto";
        scale = 1;
      }
    ];
    # Reloading the compositor clears runtime monitor rules. Restore the kept
    # layout (or the current automatic profile) without cancelling its preview.
    wayland.windowManager.hyprland.settings.on = lib.mkIf cfg.hyprland.enable [
      {
        _args = [
          "config.reloaded"
          (lib.generators.mkLuaInline ''
            function()
              hl.exec_cmd("${cfg.package}/bin/hypr-display reload")
            end'')
        ];
      }
    ];

    systemd.user.services.display-profiles = {
      Unit = {
        Description = "Automatic displays and preview rollback";
        After = ["graphical-session.target"];
        PartOf = ["graphical-session.target"];
      };
      Service = {
        ExecStart = "${cfg.package}/bin/hypr-display watch";
        Restart = "on-failure";
        RestartSec = 2;
      };
      Install.WantedBy = ["graphical-session.target"];
    };
  };
}
