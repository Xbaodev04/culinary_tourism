import useToastFeedback from "../hooks/useToastFeedback.js";
import React, { useCallback, useEffect, useRef, useState } from "react";
import axiosClient from "../../../api/axiosClient.js";
import { apiErrorMessage } from "../utils/assetUrl.js";

const API_BASE = new URL(axiosClient.defaults.baseURL, window.location.origin)
  .toString()
  .replace(/\/$/, "");

/** Listen to current POI audio without knowing MD5 names; download via protected API. */
export default function AudioPreviewPanel({
  poiId,
  compact = false,
  onlyLang = null,
  studio = false,
}) {
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useToastFeedback("error");
  const [playbackError, setPlaybackError] = useState({});
  const players = useRef([]);
  const [language, setLanguage] = useState("");
  const activeRequest = useRef(null);

  const refresh = useCallback(async () => {
    if (!poiId) return;
    activeRequest.current?.abort();
    const controller = new AbortController();
    activeRequest.current = controller;
    const signal = controller.signal;
    setLoading(true);
    setError("");
    setPlaybackError({});
    try {
      const { data } = await axiosClient.get(
        `/admin/pois/${encodeURIComponent(poiId)}/audios`,
        { signal },
      );
      if (!signal?.aborted) setResult(data);
    } catch (err) {
      if (!signal?.aborted)
        setError(apiErrorMessage(err, "Không lấy được file âm thanh"));
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [poiId]);

  useEffect(() => {
    setResult(null);
    players.current = [];
    refresh();
    return () => {
      activeRequest.current?.abort();
      Object.values(players.current).forEach((player) => player?.pause());
    };
  }, [refresh]);

  const visibleItems =
    result?.items?.filter((item) => !onlyLang || item.lang === onlyLang) || [];
  const ready = visibleItems.filter((item) => item.ready);
  const missing = visibleItems.filter((item) => !item.ready);

  const selected = ready.find((item) => item.lang === language) || ready[0];

  return (
    <section
      className={
        studio
          ? "audio-preview-content"
          : `${compact ? "mt-3" : "mt-4"} rounded-lg border border-teal-200 bg-teal-50 p-3`
      }
      aria-label="Nghe và tải file âm thanh"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3
            className={
              studio
                ? "text-lg font-bold text-slate-900"
                : "text-sm font-bold text-teal-950"
            }
          >
            {studio
              ? result?.poi_name || "Audio của quán"
              : "Nghe thử / Tải MP3"}
          </h3>
          <p className="text-xs text-teal-900">
            {result
              ? `${studio ? "" : result.poi_name + " · "}${ready.length}/${visibleItems.length} ngôn ngữ có file`
              : "Audio hiện tại của quán trong hệ thống"}
          </p>
        </div>
      </div>
      {error && (
        <p role="alert" className="mt-2 text-sm text-red-700">
          {error}
        </p>
      )}
      {loading && !result && (
        <p className="mt-2 text-sm text-slate-600">Đang lấy danh sách MP3…</p>
      )}
      {result && ready.length === 0 && (
        <p className="mt-3 rounded bg-white p-3 text-sm text-slate-700">
          Chưa có MP3 hoàn chỉnh cho{" "}
          {onlyLang === "en" ? "tiếng Anh" : "ngôn ngữ này"} trên server. Bấm{" "}
          <strong>Làm mới audio</strong> sau khi tác vụ hoàn tất.
        </p>
      )}
      {ready.length > 0 && (
        <div className="mt-3 space-y-3">
          {(studio ? [selected] : ready).map((item) => (
            <div
              key={item.lang}
              className={
                studio
                  ? "audio-player-surface"
                  : "rounded-lg border border-teal-100 bg-white p-3"
              }
            >
              <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                <div>
                  <div className="text-sm font-semibold text-slate-900">
                    {item.name}{" "}
                    <span className="font-normal text-slate-500">
                      ({item.lang})
                    </span>
                  </div>
                  <div className="text-xs text-slate-500">
                    {item.voice} · {Math.round((item.size_bytes || 0) / 1024)}{" "}
                    KB
                  </div>
                </div>
                <a
                  href={`${API_BASE}/admin/pois/${encodeURIComponent(poiId)}/audios/${encodeURIComponent(item.lang)}/download`}
                  className={
                    studio
                      ? "audio-small-button"
                      : "rounded bg-teal-700 px-3 py-1.5 text-xs font-semibold text-white hover:bg-teal-800"
                  }
                  aria-label={`Tải MP3 ${item.name}`}
                >
                  Tải MP3
                </a>
              </div>
              {/* Normal media loading supports the protected route's redirect to the public CDN. */}
              <audio
                ref={(el) => {
                  if (el) players.current[item.lang] = el;
                }}
                className="w-full"
                controls
                preload="metadata"
                src={`${API_BASE}/admin/pois/${encodeURIComponent(poiId)}/audios/${encodeURIComponent(item.lang)}/play`}
                aria-label={`Nghe audio ${item.name}`}
                onPlay={() => {
                  Object.entries(players.current).forEach(([lang, player]) => {
                    if (lang !== item.lang && player && !player.paused)
                      player.pause();
                  });
                }}
                onError={(event) => {
                  const mediaError = event.currentTarget.error;
                  const hint =
                    mediaError?.code === 3
                      ? "Không giải mã được âm thanh. Hãy thử tải MP3 để kiểm tra file."
                      : mediaError?.code === 4
                        ? "Không mở được nguồn âm thanh. Hãy thử tải MP3 hoặc đăng nhập lại rồi thử lại."
                        : "Không tải được âm thanh. Kiểm tra kết nối và thử lại.";
                  setPlaybackError((prev) => ({ ...prev, [item.lang]: hint }));
                }}
                onCanPlay={() =>
                  setPlaybackError((prev) => ({ ...prev, [item.lang]: null }))
                }
                onStalled={() =>
                  setPlaybackError((prev) => ({
                    ...prev,
                    [item.lang]:
                      "Âm thanh đang tải chậm. Vui lòng chờ hoặc thử tải MP3.",
                  }))
                }
              >
                Trình duyệt không hỗ trợ phát MP3.
              </audio>
              {playbackError[item.lang] && (
                <p role="alert" className="mt-2 text-xs text-red-700">
                  {playbackError[item.lang]}
                </p>
              )}
            </div>
          ))}
        </div>
      )}
      {studio && result && (
        <div className="audio-ready-list">
          <h4 className="font-semibold">Các bản ngôn ngữ</h4>
          <ul>
            {visibleItems.map((item) => (
              <li key={item.lang}>
                <button
                  type="button"
                  disabled={!item.ready}
                  aria-pressed={selected?.lang === item.lang}
                  onClick={() => setLanguage(item.lang)}
                >
                  <span>{item.name}</span>
                  <span
                    className={
                      item.ready ? "audio-ready-badge" : "audio-missing-badge"
                    }
                  >
                    {item.ready ? "Sẵn sàng" : "Chưa có audio"}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
      {result && missing.length > 0 && (
        <p className="mt-3 text-xs text-slate-600">
          Chưa có file: {missing.map((item) => item.name).join(", ")}.
          {ready.length > 0
            ? " Có thể tạo thêm các ngôn ngữ này ở form bên trên."
            : ""}
        </p>
      )}
      <p className="mt-2 text-xs text-slate-500">
        Bạn nghe được MP3 đã lưu ngay cả khi tác vụ cũ không còn trong lịch sử.
        Không cần kích hoạt công khai quán.
      </p>
    </section>
  );
}
