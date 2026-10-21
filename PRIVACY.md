# Privacy

This plugin runs a local MCP server on your own computer. It is an unofficial community project and is not operated as a service by anyone.

## What it collects

Nothing. The plugin has no server, account, analytics or telemetry of its own. Its author never receives any of your data.

## Where your data goes

- **Linnworks.** Your Linnworks Application ID, Application Secret and installation token are sent only to Linnworks (`api.linnworks.net` and the server address Linnworks returns) to sign in. Every tool call then reads or writes data in your own Linnworks account.
- **Shopify, only if you set it up.** The optional `repair_channel_listing_images` tool sends requests to your own Shopify store's Admin API using the token you provide. If you don't provide one, nothing is sent to Shopify.
- **Claude.** Tool results are returned to Claude in your conversation, like any other MCP tool. How Anthropic handles conversation data is covered by Anthropic's own privacy policy.

No data is sent anywhere else.

## Where your credentials are stored

Claude Code stores the values you enter in the plugin's settings. Those marked sensitive (the Linnworks secret and token, and Shopify access) go in your system's secure storage rather than in a settings file. The plugin does not write credentials anywhere.

## Contact

Questions or concerns: open an issue at https://github.com/josephgurney/linnworks-claude-plugin/issues.
