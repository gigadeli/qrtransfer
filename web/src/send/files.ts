/** 選ばれたファイル・フォルダを、送るもの（SendItem）の木にする。 */
import type { SendItem } from "../lib/pack";

type DirItem = SendItem & { kind: "dir" };

const seconds = (f: File) => Math.floor((f.lastModified || Date.now()) / 1000);

function fileItem(f: File): SendItem {
  return { kind: "file", name: f.name, data: f, mtime: seconds(f) };
}

/** ファイル選択（複数可）で選ばれたもの */
export function fromFileList(files: FileList | File[]): SendItem[] {
  return [...files].map(fileItem);
}

/**
 * フォルダ選択（webkitdirectory）で選ばれたもの。ファイルごとに "フォルダ/中/名前" の相対パスが付いている。
 * この方法では空のフォルダは含まれない（ブラウザが知らせないため）。
 */
export function fromDirectoryInput(files: FileList | File[]): SendItem[] {
  const roots = new Map<string, DirItem>();
  const now = Math.floor(Date.now() / 1000);
  for (const f of files) {
    const parts = (f.webkitRelativePath || f.name).split("/").filter(Boolean);
    if (parts.length < 2) continue;
    const child = (parent: DirItem | null, name: string): DirItem => {
      const found = parent ? parent.children.find((c): c is DirItem => c.kind === "dir" && c.name === name) : roots.get(name);
      if (found) return found;
      const made: DirItem = { kind: "dir", name, children: [], mtime: now };
      if (parent) parent.children.push(made);
      else roots.set(name, made);
      return made;
    };
    let dir = child(null, parts[0]);
    for (const name of parts.slice(1, -1)) dir = child(dir, name);
    dir.children.push(fileItem(f));
  }
  return [...roots.values()];
}

function readEntries(reader: FileSystemDirectoryReader): Promise<FileSystemEntry[]> {
  return new Promise((resolve, reject) => reader.readEntries(resolve, reject));
}

async function fromEntry(entry: FileSystemEntry): Promise<SendItem | null> {
  if (entry.isFile) {
    const f = await new Promise<File>((resolve, reject) => (entry as FileSystemFileEntry).file(resolve, reject));
    return fileItem(f);
  }
  if (entry.isDirectory) {
    const reader = (entry as FileSystemDirectoryEntry).createReader();
    const children: SendItem[] = [];
    // readEntries は一度に全部を返さないことがあるので、空になるまで読む
    for (;;) {
      const batch = await readEntries(reader);
      if (!batch.length) break;
      for (const e of batch) {
        const it = await fromEntry(e);
        if (it) children.push(it);
      }
    }
    return { kind: "dir", name: entry.name, children, mtime: Math.floor(Date.now() / 1000) };
  }
  return null;
}

/** ドラッグ＆ドロップされたもの（フォルダは中身ごと。空のフォルダも含む） */
export async function fromDataTransfer(dt: DataTransfer): Promise<SendItem[]> {
  const entries = [...dt.items]
    .filter((i) => i.kind === "file")
    .map((i) => i.webkitGetAsEntry?.())
    .filter((e): e is FileSystemEntry => !!e);
  if (!entries.length) return fromFileList(dt.files);
  const out: SendItem[] = [];
  for (const e of entries) {
    const it = await fromEntry(e);
    if (it) out.push(it);
  }
  return out;
}

/** フォルダ選択が使えるか（iPhone・iPad の Safari などでは使えない） */
export function canPickDirectory(): boolean {
  return "webkitdirectory" in document.createElement("input") && !/iPhone|iPad|iPod|Android/.test(navigator.userAgent);
}
