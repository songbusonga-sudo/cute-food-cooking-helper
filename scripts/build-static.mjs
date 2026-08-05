import { cp, mkdir, rm, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';

const root = resolve(import.meta.dirname, '..');
const dist = resolve(root, 'dist');

await rm(dist, { recursive: true, force: true });
await mkdir(dist, { recursive: true });
await cp(resolve(root, 'index.html'), resolve(dist, 'index.html'));
const apiOrigin = process.env.PUBLIC_API_ORIGIN?.replace(/\/+$/, '');
await writeFile(
  resolve(dist, 'config.js'),
  apiOrigin
    ? `window.CUTE_FOOD_CONFIG = { API_ORIGIN: ${JSON.stringify(apiOrigin)} };\n`
    : 'window.CUTE_FOOD_CONFIG = { API_ORIGIN: window.location.origin };\n',
);

console.log('Static frontend built in dist/.');
