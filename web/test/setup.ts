// Node には ImageData が無いので、テスト用に最低限のものを用意する
if (typeof globalThis.ImageData === "undefined") {
  class ImageDataPolyfill {
    data: Uint8ClampedArray;
    width: number;
    height: number;
    constructor(a: number | Uint8ClampedArray, b: number, c?: number) {
      if (typeof a === "number") {
        this.width = a;
        this.height = b;
        this.data = new Uint8ClampedArray(a * b * 4);
      } else {
        this.data = a;
        this.width = b;
        this.height = c ?? a.length / 4 / b;
      }
    }
  }
  (globalThis as unknown as { ImageData: unknown }).ImageData = ImageDataPolyfill;
}
