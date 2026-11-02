// Rewrites a GitHub release's Homebrew Channel feed so every asset URL points at the
// public mirror (Cloudflare R2 at luma.elischleifer.com): <base>/releases/<tag>/<file>. The IPK bytes, hash and size are unchanged.
//
//   node tools/mirror-feed.mjs <repo.json> <tag> <base-url>  → prints the mirrored feed
import fs from 'node:fs/promises';
import { pathToFileURL } from 'node:url';

export function mirrorFeed(feed, tag, base) {
  if (!/^v\d+\.\d+\.\d+$/.test(tag)) throw new Error(`Invalid release tag: ${tag}`);
  const origin = String(base).replace(/\/+$/, '');
  if (!/^https:\/\/[\w.-]+$/.test(origin)) throw new Error(`Base must be an https origin: ${base}`);
  const target = `${origin}/releases/${tag}/`;
  const rewrite = (url) => {
    const match =
      /^https:\/\/github\.com\/[\w.-]+\/[\w.-]+\/releases\/download\/([^/]+)\/([^/?#]+)$/.exec(
        url ?? '',
      );
    if (!match || match[1] !== tag) throw new Error(`Unexpected release asset URL: ${url}`);
    return target + match[2];
  };
  const mirrored = structuredClone(feed);
  if (!Array.isArray(mirrored.packages) || mirrored.packages.length === 0)
    throw new Error('The feed has no packages.');
  for (const entry of mirrored.packages) {
    if (entry.fullDescriptionUrl) entry.fullDescriptionUrl = rewrite(entry.fullDescriptionUrl);
    if (entry.iconUri) entry.iconUri = rewrite(entry.iconUri);
    const manifest = entry.manifest ?? {};
    if (`v${manifest.version}` !== tag)
      throw new Error(`Feed version ${manifest.version} does not match ${tag}.`);
    if (manifest.iconUri) manifest.iconUri = rewrite(manifest.iconUri);
    manifest.ipkUrl = rewrite(manifest.ipkUrl);
  }
  return mirrored;
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? '').href) {
  const [file, tag, base] = process.argv.slice(2);
  if (!file || !tag || !base) {
    console.error('Usage: node tools/mirror-feed.mjs <repo.json> <tag> <base-url>');
    process.exit(2);
  }
  const feed = JSON.parse(await fs.readFile(file, 'utf8'));
  process.stdout.write(JSON.stringify(mirrorFeed(feed, tag, base), null, 2) + '\n');
}
