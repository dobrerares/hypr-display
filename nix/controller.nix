# Builds the hypr-display command with a fixed set of profiles.
{
  pkgs,
  profiles,
  noctaliaPackage ? null,
  hyprlandPackage ? pkgs.hyprland,
}: let
  profileConfig = pkgs.writeText "display-profiles.json" (builtins.toJSON profiles);
in
  pkgs.writeShellApplication {
    name = "hypr-display";
    runtimeInputs =
      [
        pkgs.python3
        hyprlandPackage # hyprctl: monitor state, Lua eval, event socket path
        pkgs.systemd
        # Present bundle: wpctl (default sink) and pw-dump (sink discovery).
        pkgs.wireplumber
        pkgs.pipewire
      ]
      # `noctalia msg` for keep-awake and Do Not Disturb. Without it the
      # command still works when noctalia is on PATH; otherwise those two steps
      # are reported and skipped.
      ++ pkgs.lib.optional (noctaliaPackage != null) noctaliaPackage;
    text = ''exec python3 ${../controller/displays.py} --profiles ${profileConfig} "$@"'';
  }
