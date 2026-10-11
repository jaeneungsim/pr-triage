import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { cpSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';
import { AssetHashType } from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';

const ROOT = resolve(__dirname, '..', '..');
const SOURCES = ['src/pr_triage', 'policy', 'prompts', 'requirements.txt'];
const SKIP = (path: string) => path.includes('__pycache__') || path.endsWith('.pyc');

function files(path: string): string[] {
  if (statSync(path).isFile()) return [path];
  return readdirSync(path).flatMap((name) => files(join(path, name)));
}

/** Hash only what goes into the bundle, so unrelated changes do not rebuild it. */
function sourceHash(): string {
  const hash = createHash('sha256');
  for (const file of SOURCES.flatMap((s) => files(join(ROOT, s))).filter((f) => !SKIP(f)).sort()) {
    hash.update(relative(ROOT, file)).update(readFileSync(file));
  }
  return hash.digest('hex');
}

function copySources(outputDir: string): void {
  cpSync(join(ROOT, 'src', 'pr_triage'), join(outputDir, 'pr_triage'), { recursive: true, filter: (p) => !SKIP(p) });
  cpSync(join(ROOT, 'policy'), join(outputDir, 'policy'), { recursive: true });
  cpSync(join(ROOT, 'prompts'), join(outputDir, 'prompts'), { recursive: true });
}

/**
 * One code bundle for every Lambda: the pr_triage package, policy, prompts and the pinned
 * dependencies built for Linux arm64. Bundles locally with uv; falls back to Docker.
 */
export function pythonCode(): lambda.Code {
  return lambda.Code.fromAsset(ROOT, {
    assetHashType: AssetHashType.CUSTOM,
    assetHash: sourceHash(),
    exclude: ['infra', '.venv', '.git', 'tests', 'scripts', 'docs', '**/__pycache__', '**/.pytest_cache', '.ruff_cache'],
    bundling: {
      image: lambda.Runtime.PYTHON_3_13.bundlingImage,
      platform: 'linux/arm64',
      command: [
        'bash', '-c',
        'pip install --no-cache-dir -r requirements.txt -t /asset-output && ' +
          'cp -r src/pr_triage /asset-output/pr_triage && cp -r policy prompts /asset-output/',
      ],
      local: {
        tryBundle(outputDir: string): boolean {
          try {
            execFileSync('uv', [
              'pip', 'install', '--quiet', '--target', outputDir,
              '--python-platform', 'aarch64-manylinux_2_28', '--python-version', '3.13',
              '--only-binary', ':all:', '-r', join(ROOT, 'requirements.txt'),
            ], { stdio: 'inherit' });
            copySources(outputDir);
            return true;
          } catch (err) {
            process.stderr.write(`local bundling failed, trying Docker: ${err}\n`);
            return false;
          }
        },
      },
    },
  });
}
