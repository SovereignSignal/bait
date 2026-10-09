export function createTokenBucket(opts: { capacity: number; refillPerSec: number; now?: () => number }) {
  const now = opts.now ?? Date.now;
  let tokens = opts.capacity;
  let last = now();
  return {
    take(): boolean {
      const t = now();
      tokens = Math.min(opts.capacity, tokens + ((t - last) / 1000) * opts.refillPerSec);
      last = t;
      if (tokens < 1) return false;
      tokens -= 1;
      return true;
    },
  };
}
