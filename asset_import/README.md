# Media library import folder

Put JPEG/PNG files here temporarily. The app mounts this folder read-only as /imports; imported files are copied into the persistent Docker volume under /data/assets and registered in SQLite.

Background example:
docker compose exec app python -m newsbot.assets --add /imports/stadium.png --type background --name Night_stadium --license CC0 --credit Photographer_name --source-url https://example.org/image

Club crest example:
docker compose exec app python -m newsbot.assets --add /imports/zenit.png --type crest --team-key zenit --name Zenit_crest --license Club_permission_or_license --aliases "Зенит;Зенит Санкт-Петербург"

The license/permission value is required metadata. Verify image usage rights before importing assets for public posts. List resources with docker compose exec app python -m newsbot.assets --list; remove one with --remove ASSET_ID.

The aliases are used to match Russian and alternate team names in match data to the correct crest. Keep one stable English key per team; add common Russian spellings with --aliases. A result card uses the least recently used enabled background, registered crests when a team match is found, and text initials when a crest is missing. Cards are portrait 1080x1350 JPEGs.
