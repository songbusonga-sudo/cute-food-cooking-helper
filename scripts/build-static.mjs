import { cp, mkdir, rm, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';

const root = resolve(import.meta.dirname, '..');
const dist = resolve(root, 'dist');

await rm(dist, { recursive: true, force: true });
await mkdir(dist, { recursive: true });
await cp(resolve(root, 'index.html'), resolve(dist, 'index.html'));
const apiOrigin = (process.env.PUBLIC_API_ORIGIN || 'http://localhost:3008').replace(/\/+$/, '');
await writeFile(
  resolve(dist, 'config.js'),
  `window.CUTE_FOOD_CONFIG = { API_ORIGIN: ${JSON.stringify(apiOrigin)} };\n`,
);

console.log('Static frontend built in dist/.');
