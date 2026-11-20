// Restarts the immich server when its worker holds a job slot it will never
// release. It runs in the immich-server image, so it reads the server's own
// dependency versions and takes the server's Redis client. Why it exists and
// when it goes: `kluster.components.apps.immich.watchdog`.
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import https from 'node:https';

const require = createRequire('/usr/src/app/server/package.json');
const Redis = require('ioredis');

const env = (key) => {
    const value = process.env[key];
    if (!value) {
        throw new Error(`${key} is not set`);
    }
    return value;
};

const NAMESPACE = env('NAMESPACE');
const SERVER_SELECTOR = env('SERVER_SELECTOR');
const MAX_JOB_AGE_SECONDS = Number(env('MAX_JOB_AGE_SECONDS'));
const FIXED_POSTGRES_MAJOR = Number(env('FIXED_POSTGRES_MAJOR'));
const PREFIX = env('QUEUE_PREFIX');
const POSTGRES_PACKAGE = env('POSTGRES_PACKAGE');

function checkStillNeeded() {
    // Read directly: the package's `exports` map hides its package.json.
    const { version } = JSON.parse(readFileSync(POSTGRES_PACKAGE, 'utf-8'));
    console.log(`immich ships postgres.js ${version}`);
    if (Number(version.split('.')[0]) >= FIXED_POSTGRES_MAJOR) {
        console.error(
            `postgres.js ${version} carries the fix for porsager/postgres#1195, so this ` +
            'watchdog is obsolete and restarts nothing: a slot that still leaks has another ' +
            'cause. Remove the layer that src/kluster/components/apps/immich/watchdog.py ' +
            'describes.');
        process.exit(1);
    }
}

/**
 * Active jobs that started more than MAX_JOB_AGE_SECONDS ago and whose BullMQ
 * lock is still held. The live lock is what tells a leaked slot apart from a
 * job orphaned by a worker that already died: BullMQ's stalled check returns
 * the latter to `wait` once its lock expires, so it needs no restart.
 */
async function findStuckJobs(redis) {
    const now = Date.now();
    const stuck = [];
    let checked = 0;
    for await (const keys of redis.scanStream({ match: `${PREFIX}:*:active`, type: 'list' })) {
        for (const key of keys) {
            const queue = key.slice(PREFIX.length + 1, -':active'.length);
            for (const id of await redis.lrange(key, 0, -1)) {
                checked++;
                const [name, processedOn] = await redis.hmget(`${PREFIX}:${queue}:${id}`, 'name', 'processedOn');
                const ageSeconds = (now - Number(processedOn)) / 1000;
                if (!processedOn || ageSeconds <= MAX_JOB_AGE_SECONDS) {
                    continue;
                }
                if (await redis.exists(`${PREFIX}:${queue}:${id}:lock`)) {
                    stuck.push({ queue, id, name, since: new Date(Number(processedOn)).toISOString() });
                }
            }
        }
    }
    console.log(`checked ${checked} active job(s), ${stuck.length} stuck past ${MAX_JOB_AGE_SECONDS}s`);
    return stuck;
}

const SA = '/var/run/secrets/kubernetes.io/serviceaccount';

function kube(method, path) {
    return new Promise((resolve, reject) => {
        const req = https.request({
            host: env('KUBERNETES_SERVICE_HOST'),
            port: env('KUBERNETES_SERVICE_PORT'),
            path,
            method,
            ca: readFileSync(`${SA}/ca.crt`),
            headers: { Authorization: `Bearer ${readFileSync(`${SA}/token`, 'utf-8')}` },
        }, res => {
            let body = '';
            res.on('data', chunk => body += chunk);
            res.on('end', () => res.statusCode < 300
                ? resolve(JSON.parse(body))
                : reject(new Error(`${method} ${path}: ${res.statusCode} ${body}`)));
        });
        req.on('error', reject);
        req.end();
    });
}

async function restartServer(stuck) {
    const jobs = stuck.map(j => `${j.queue}/${j.name} (job ${j.id}, running since ${j.since})`).join('; ');
    console.log(`the worker holds job slots it will never release (porsager/postgres#1195): ${jobs}`);
    const pods = await kube('GET',
        `/api/v1/namespaces/${NAMESPACE}/pods?labelSelector=${encodeURIComponent(SERVER_SELECTOR)}`);
    for (const pod of pods.items) {
        console.log(`deleting pod ${pod.metadata.name}; its ReplicaSet recreates it`);
        await kube('DELETE', `/api/v1/namespaces/${NAMESPACE}/pods/${pod.metadata.name}`);
    }
}

checkStillNeeded();
const redis = new Redis({
    host: env('REDIS_HOSTNAME'),
    port: Number(env('REDIS_PORT')),
    password: env('REDIS_PASSWORD'),
});
try {
    console.log('scanning immich job queues for leaked slots');
    const stuck = await findStuckJobs(redis);
    if (stuck.length > 0) {
        await restartServer(stuck);
        // The run fails, so the scheduled-job family's failed alert reports
        // the leak; the next run that finds none resolves it.
        console.error('restarted the server to release leaked job slots');
        process.exitCode = 1;
    }
} finally {
    redis.disconnect();
}
