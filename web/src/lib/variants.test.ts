import { test } from "node:test";
import assert from "node:assert/strict";
import { variantLabel, variants } from "./variants";

test("a trailing chain in parentheses is the chain, other parentheses stay", () => {
  assert.equal(variantLabel("Clanker Static Fee Hook (Base)"), "Clanker Static Fee Hook");
  assert.equal(variantLabel("Bonker Static Fee Hook (Robinhood Chain)"), "Bonker Static Fee Hook");
  assert.equal(variantLabel("Foo Hook (X Layer)"), "Foo Hook");
  assert.equal(variantLabel("Clanker Dynamic Fee Hook (V2)"), "Clanker Dynamic Fee Hook (V2)");
  assert.equal(variantLabel("Aegis"), "Aegis");
});

test("a merged team shows its deployments, per-chain twins as one chip", () => {
  const v = variants([
    { name: "Aegis", chainId: 1 },
    { name: "Aegis", chainId: 8453 },
    { name: "AegisHook", chainId: 57073 },
    { name: "Aegis v2 Hook (Unichain)", chainId: 130 },
    { name: "Aegis v3 Hook (Base)", chainId: 8453 },
    { name: "Aegis v3 Hook (Unichain)", chainId: 130 },
  ]);
  assert.deepEqual(
    v.map((x) => [x.label, x.chains]),
    [
      ["Aegis", [1, 8453, 57073]],
      ["Aegis v3 Hook", [8453, 130]],
      ["Aegis v2 Hook", [130]],
    ],
  );
});

test("one name, unnamed or unlisted hooks: no chips", () => {
  assert.deepEqual(variants([{ name: "ArrakisPrivateHook", chainId: 1 }, { name: "ArrakisPrivateHook", chainId: 8453 }]), []);
  assert.deepEqual(variants([{ name: "Unnamed hook", chainId: 1 }, { name: "Unlisted hook 0x1234…abcd", chainId: 1 }, { name: "Zora Hook", chainId: 8453 }]), []);
});
