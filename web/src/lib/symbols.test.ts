import assert from "node:assert/strict";
import { test } from "node:test";
import { cleanSymbol, decodeSymbol } from "./symbols";

const word = (n: number) => n.toString(16).padStart(64, "0");
const strHex = (s: string) => Buffer.from(s, "utf8").toString("hex").padEnd(64, "0");

test("decodes an ABI string symbol()", () => {
  assert.equal(decodeSymbol("0x" + word(32) + word(4) + strHex("WETH")), "WETH");
  assert.equal(decodeSymbol("0x" + word(32) + word(4) + strHex("USDG")), "USDG");
});

test("decodes a bytes32 symbol() (MKR-style)", () => {
  assert.equal(decodeSymbol("0x" + strHex("MKR")), "MKR");
});

test("rejects empty, short, and junk results", () => {
  assert.equal(decodeSymbol(null), null);
  assert.equal(decodeSymbol("0x"), null);
  assert.equal(decodeSymbol("0x" + word(0)), null);
  assert.equal(cleanSymbol("A".repeat(40)), null);          // absurdly long names are not symbols
  assert.equal(cleanSymbol(" PEPE\u0000 "), "PEPE");
});
