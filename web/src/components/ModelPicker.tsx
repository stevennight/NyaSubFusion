import { useEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent } from "react";
import { createPortal } from "react-dom";
import type { ModelOption } from "../types";

export interface ModelPickerProps {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  title?: string;
  models?: ModelOption[];
  loading?: boolean;
  error?: string;
  loaded?: boolean;
  onFetch?: () => void | Promise<void>;
  filterModality?: "image" | "audio";
  allowClear?: boolean;
  className?: string;
  style?: CSSProperties;
}

const MODALITY_LABEL: Record<string, string> = {
  image: "图",
  audio: "音",
  video: "视",
};

const MODALITY_FILTER_TEXT: Record<"image" | "audio", string> = {
  image: "仅支持视觉",
  audio: "仅支持音频",
};

const MAX_ROWS = 120;

function formatCtx(ctx?: number): string {
  if (!ctx || ctx <= 0) return "";
  if (ctx >= 1_000_000) {
    const m = ctx / 1_000_000;
    return `${Number.isInteger(m) ? m : m.toFixed(1)}M`;
  }
  if (ctx >= 1000) return `${Math.round(ctx / 1000)}K`;
  return String(ctx);
}

function scoreModel(m: ModelOption, tokens: string[], rawQuery: string): number {
  const id = m.id.toLowerCase();
  const hay = `${m.id} ${m.name} ${m.owned_by ?? ""}`.toLowerCase();
  for (const t of tokens) {
    if (!hay.includes(t)) return -1;
  }
  if (!rawQuery) return m.default ? 0 : 1;
  if (id === rawQuery) return 0;
  if (id.startsWith(rawQuery)) return 1;
  if (id.includes(rawQuery)) return 2;
  if (tokens.every((t) => id.includes(t))) return 3;
  return 4;
}

export function ModelPicker({
  value,
  onChange,
  placeholder,
  title,
  models = [],
  loading = false,
  error = "",
  loaded = false,
  onFetch,
  filterModality,
  allowClear = false,
  className,
  style,
}: ModelPickerProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [onlyModality, setOnlyModality] = useState(Boolean(filterModality));
  const [active, setActive] = useState(0);
  const [pos, setPos] = useState<{ left: number; width: number; top?: number; bottom?: number; maxHeight: number }>({
    left: 0, width: 320, top: 0, maxHeight: 320,
  });

  const wrapRef = useRef<HTMLDivElement>(null);
  const popRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const hasMatchingModality = useMemo(
    () => Boolean(filterModality && models.some((m) => m.modalities?.includes(filterModality))),
    [models, filterModality],
  );

  const modalityCount = useMemo(() => {
    if (!filterModality) return 0;
    return models.filter((m) => m.modalities?.includes(filterModality)).length;
  }, [models, filterModality]);

  const effectiveModFilter = Boolean(filterModality && onlyModality && hasMatchingModality);

  const filtered = useMemo(() => {
    const raw = query.trim().toLowerCase();
    const tokens = raw ? raw.split(/\s+/).filter(Boolean) : [];
    const pool = effectiveModFilter && filterModality
      ? models.filter((m) => m.modalities?.includes(filterModality))
      : models;
    const scored: { m: ModelOption; s: number; idx: number }[] = [];
    for (let i = 0; i < pool.length; i++) {
      const m = pool[i];
      const s = scoreModel(m, tokens, raw);
      if (s >= 0) scored.push({ m, s, idx: i });
    }
    scored.sort((a, b) => a.s - b.s || a.idx - b.idx);
    return scored.map((x) => x.m);
  }, [models, query, effectiveModFilter, filterModality]);

  const visible = useMemo(() => filtered.slice(0, MAX_ROWS), [filtered]);

  const updatePos = () => {
    const el = wrapRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const width = Math.min(Math.max(r.width, 360), window.innerWidth - 24);
    const left = Math.max(12, Math.min(r.left, window.innerWidth - width - 12));
    const spaceBelow = window.innerHeight - r.bottom - 12;
    const spaceAbove = r.top - 12;
    if (spaceBelow < 220 && spaceAbove > spaceBelow) {
      setPos({
        left,
        width,
        bottom: window.innerHeight - r.top + 4,
        maxHeight: Math.max(160, Math.min(360, spaceAbove)),
      });
    } else {
      setPos({
        left,
        width,
        top: r.bottom + 4,
        maxHeight: Math.max(180, Math.min(360, spaceBelow)),
      });
    }
  };

  const openPicker = () => {
    updatePos();
    setQuery("");
    setActive(0);
    setOpen(true);
    if (!loaded && !loading && onFetch) {
      void onFetch();
    }
  };

  useEffect(() => {
    if (!open) return;
    updatePos();
    const id = requestAnimationFrame(() => searchRef.current?.focus());
    const onDown = (e: MouseEvent) => {
      const target = e.target as Node | null;
      if (target && (wrapRef.current?.contains(target) || popRef.current?.contains(target))) return;
      setOpen(false);
    };
    const onScroll = (e: Event) => {
      const target = e.target as Node | null;
      if (target && popRef.current?.contains(target)) return;
      updatePos();
    };
    const onResize = () => updatePos();
    document.addEventListener("mousedown", onDown);
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", onResize);
    return () => {
      cancelAnimationFrame(id);
      document.removeEventListener("mousedown", onDown);
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", onResize);
    };
  }, [open]);

  useEffect(() => {
    setActive((a) => (visible.length === 0 ? 0 : Math.min(a, visible.length - 1)));
  }, [visible.length]);

  useEffect(() => {
    if (!open || !listRef.current) return;
    const row = listRef.current.children[active] as HTMLElement | undefined;
    row?.scrollIntoView({ block: "nearest" });
  }, [active, open]);

  const choose = (mid: string) => {
    onChange(mid);
    setOpen(false);
  };

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Escape") {
      if (open) {
        e.preventDefault();
        setOpen(false);
      }
      return;
    }
    if (!open) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        openPicker();
      }
      return;
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (visible.length) setActive((a) => (a + 1) % visible.length);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (visible.length) setActive((a) => (a - 1 + visible.length) % visible.length);
    } else if (e.key === "Enter") {
      if (visible[active]) {
        e.preventDefault();
        choose(visible[active].id);
      }
    }
  };

  return (
    <div ref={wrapRef} className={`mp-wrap${className ? ` ${className}` : ""}`} style={style}>
      <input
        className="inp mp-inp"
        value={value}
        placeholder={placeholder}
        title={title}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown" && !open) {
            e.preventDefault();
            openPicker();
          } else if (e.key === "Escape" && open) {
            e.preventDefault();
            setOpen(false);
          }
        }}
      />
      {allowClear && value && (
        <button
          type="button"
          className="mp-btn mp-clr"
          title="清空模型"
          aria-label="清空模型"
          onClick={() => onChange("")}
        >
          ×
        </button>
      )}
      <button
        type="button"
        className="mp-btn mp-trig"
        title="从上游获取并选择模型"
        aria-label="选择模型"
        aria-expanded={open}
        onClick={() => (open ? setOpen(false) : openPicker())}
      >
        {loading ? "…" : "▾"}
      </button>

      {open &&
        createPortal(
          <div
            ref={popRef}
            className="mp-pop"
            style={{
              left: pos.left,
              width: pos.width,
              top: pos.top,
              bottom: pos.bottom,
              maxHeight: pos.maxHeight,
            }}
          >
            <div className="mp-head">
              <input
                ref={searchRef}
                className="inp mp-search"
                value={query}
                placeholder="搜索模型（支持空格多词，如 6.2 sol / flash）"
                onChange={(e) => {
                  setQuery(e.target.value);
                  setActive(0);
                }}
                onKeyDown={onKeyDown}
              />
              {onFetch && (
                <button
                  type="button"
                  className="btn sm"
                  disabled={loading}
                  title="重新从上游拉取模型列表"
                  onClick={() => void onFetch()}
                >
                  {loading ? "读取中…" : "刷新"}
                </button>
              )}
            </div>

            {(filterModality && hasMatchingModality) || loaded ? (
              <div className="mp-meta">
                {filterModality && hasMatchingModality && (
                  <button
                    type="button"
                    className={`chip click${onlyModality ? " mp-chip-on" : ""}`}
                    onClick={() => {
                      setOnlyModality((v) => !v);
                      setActive(0);
                    }}
                  >
                    {MODALITY_FILTER_TEXT[filterModality]} ({modalityCount})
                  </button>
                )}
                <span className="mp-count">
                  {loading
                    ? "正在获取模型列表…"
                    : `共 ${filtered.length} 个${effectiveModFilter ? "匹配模态" : "可用"}模型`}
                </span>
              </div>
            ) : null}

            {error && <div className="mp-err">{error}</div>}

            <div ref={listRef} className="mp-list" role="listbox">
              {loading && visible.length === 0 && (
                <div className="mp-empty">正在从上游拉取模型列表…</div>
              )}
              {!loading && visible.length === 0 && (
                <div className="mp-empty">
                  {models.length === 0
                    ? (error ? "获取失败，可直接在输入框手动填写模型名" : "暂无模型，可点击「刷新」或直接手填模型名")
                    : "没有匹配的模型，可清除筛选或直接手填模型名"}
                </div>
              )}
              {visible.map((m, i) => {
                const isSelected = m.id === value;
                const isActive = i === active;
                const ctx = formatCtx(m.context_length);
                const mods = (m.modalities ?? []).filter((x) => x !== "text");
                return (
                  <button
                    key={m.id}
                    type="button"
                    role="option"
                    aria-selected={isSelected}
                    className={`mp-item${isActive ? " active" : ""}${isSelected ? " sel" : ""}`}
                    onMouseEnter={() => setActive(i)}
                    onClick={() => choose(m.id)}
                  >
                    <div className="mp-main">
                      <span className="mp-id">{m.id}</span>
                      {m.name && m.name !== m.id && <span className="mp-name">{m.name}</span>}
                    </div>
                    <div className="mp-badges">
                      {m.default && <span className="mp-tag def">默认</span>}
                      {mods.slice(0, 3).map((md) => (
                        <span key={md} className="mp-tag">{MODALITY_LABEL[md] ?? md}</span>
                      ))}
                      {ctx && <span className="mp-tag">{ctx}</span>}
                    </div>
                  </button>
                );
              })}
              {filtered.length > MAX_ROWS && (
                <div className="mp-more">仅显示前 {MAX_ROWS} 条（共 {filtered.length} 个，继续输入关键词缩小范围）</div>
              )}
            </div>
          </div>,
          document.body,
        )}
    </div>
  );
}
