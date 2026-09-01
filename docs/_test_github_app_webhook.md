# Test PR — GitHub App webhook + boardman[bot] verification

Throwaway PR to confirm, after the GITHUB_AUTH_MODE=github_app cutover (PR #127):
- the GitHub App webhook secret verifies (delivery returns 2xx, not 401)
- Boardman posts as `boardman[bot]`

Safe to close without merging. Delete this file on close.
