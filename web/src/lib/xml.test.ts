import { test } from "node:test";
import assert from "node:assert/strict";
import { escXml } from "./xml";

test("escXml escapes markup and strips XML-illegal control characters", () => {
  assert.equal(escXml(`a<b>&"c"'`), "a&lt;b&gt;&amp;&quot;c&quot;&apos;");
  assert.equal(escXml("x\u0000y\u0008z\u000Bq\u001F"), "xyzq");
  assert.equal(escXml("tab\tnl\nok"), "tab\tnl\nok");
});
