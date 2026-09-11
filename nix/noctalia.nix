# Registers the bundled Noctalia plugin. Import alongside Noctalia's own Home
# Manager module; the plugin still needs its bar widget placed (see README).
{
  config,
  lib,
  ...
}: {
  config = lib.mkIf config.services.displayProfiles.enable {
    programs.noctalia.settings.plugins = {
      source = [
        {
          name = "hypr-display";
          kind = "path";
          location = "${../plugin}";
        }
      ];
      enabled = ["rdobre/displays"];
    };
  };
}
