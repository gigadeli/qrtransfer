import { describe, expect, it } from "vitest";
import { TEXT_PREVIEW_MAX, previewOf, previewText } from "../src/lib/preview";

describe("プレビュー", () => {
  it("画像・動画・音声・テキストを判別する", () => {
    expect(previewOf("写真.JPG")).toEqual({ kind: "image", mime: "image/jpeg" });
    expect(previewOf("IMG_0001.HEIC")?.kind).toBe("image");
    expect(previewOf("clip.mov")).toEqual({ kind: "video", mime: "video/quicktime" });
    expect(previewOf("a.m4a")?.kind).toBe("audio");
    expect(previewOf("memo.md")?.kind).toBe("text");
    expect(previewOf("noext")).toBeNull();
    expect(previewOf("archive.zip")).toBeNull();
  });

  it("ページとして動き得る種類は表示しない", () => {
    for (const name of ["x.html", "x.htm", "x.svg", "x.xml", "x.xhtml", "x.js", "x.pdf", "x.svg.png.html"]) {
      expect(previewOf(name), name).toBeNull();
    }
  });

  it("テキストは先頭だけ表示する（BOM は除く）", () => {
    const bom = new Uint8Array([0xef, 0xbb, 0xbf, ...new TextEncoder().encode("日本語")]);
    expect(previewText(bom)).toEqual({ text: "日本語", truncated: false });
    const big = previewText(new Uint8Array(TEXT_PREVIEW_MAX + 10).fill(0x61));
    expect(big.truncated).toBe(true);
    expect(big.text.length).toBe(TEXT_PREVIEW_MAX);
  });
});
