"""Проверяемое best-effort управление HTML5-видео."""

from __future__ import annotations

import math
from typing import Any


class VideoController:
    async def state(self, page: Any) -> dict:
        return await page.evaluate(r"""
        () => {
          const v = document.querySelector('video');
          if (!v) return {has_video:false, url:location.href};
          return {
            has_video:true,
            url:location.href,
            src:v.currentSrc || v.src || '',
            currentTime:Number.isFinite(v.currentTime) ? v.currentTime : 0,
            duration:Number.isFinite(v.duration) ? v.duration : null,
            paused:v.paused,
            ended:v.ended,
            muted:v.muted,
            volume:v.volume,
            playbackRate:v.playbackRate,
            readyState:v.readyState,
            textTracks:[...v.textTracks].map(t=>({
              kind:t.kind,
              label:t.label,
              language:t.language,
              mode:t.mode
            }))
          };
        }
        """)

    @staticmethod
    def _number(value: Any, name: str, low: float, high: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name}: требуется число")
        if not math.isfinite(number) or not low <= number <= high:
            raise ValueError(f"{name}: допустимый диапазон {low}…{high}")
        return number

    async def action(self, page: Any, action: str, param: Any = None) -> dict:
        action = (action or "").strip().casefold()

        if action == "state":
            return {"ok": True, "state": await self.state(page)}

        aliases = {
            "volume": "volume_set",
            "seek": "seek_to",
            "speed": "set_speed",
            "fullscreen": "fullscreen_enter",
        }
        action = aliases.get(action, action)

        supported = {
            "play", "pause", "stop", "restart",
            "mute", "unmute", "toggle_mute",
            "volume_set", "volume_up", "volume_down",
            "seek_to", "seek_forward", "seek_backward",
            "set_speed", "subtitles_on", "subtitles_off",
            "subtitles_toggle", "fullscreen_enter", "fullscreen_exit",
        }

        if action not in supported:
            return {
                "ok": False,
                "error": (
                    "Неизвестное или сайтозависимое действие. "
                    "Поддерживаются действия HTML5-видео; качество и "
                    "режимы сайтов универсально не подтверждаются."
                ),
            }

        value = None
        if action == "volume_set":
            # Правка №7: универсальный разбор формата — принимаем и доли
            # 0..1, и проценты 0..100 (целые вида 50 раньше падали с
            # ValueError "допустимый диапазон 0…1"). Результат — всегда доля.
            raw = str(param).strip().rstrip("%").replace(",", ".")
            number = self._number(raw, "Громкость", 0, 100)
            value = number / 100.0 if number > 1.0 else number
        elif action in ("volume_up", "volume_down"):
            value = self._number(
                param if param is not None else 0.1,
                "Шаг громкости", 0, 1,
            )
        elif action == "seek_to":
            value = self._number(param, "Позиция", 0, 10**9)
        elif action in ("seek_forward", "seek_backward"):
            value = self._number(
                param if param is not None else 10,
                "Секунды", 0, 10**6,
            )
        elif action == "set_speed":
            value = self._number(param, "Скорость", 0.25, 4)

        result = await page.evaluate(r"""
        async ({action, value}) => {
          const v = document.querySelector('video');
          if (!v) return {ok:false,error:'На странице нет HTML5 video'};

          try {
            switch (action) {
              case 'play':
              case 'restart':
                if (action === 'restart') v.currentTime = 0;
                await v.play();
                break;
              case 'pause':
                v.pause();
                break;
              case 'stop':
                v.pause();
                v.currentTime = 0;
                break;
              case 'mute':
                v.muted = true;
                break;
              case 'unmute':
                v.muted = false;
                break;
              case 'toggle_mute':
                v.muted = !v.muted;
                break;
              case 'volume_set':
                v.volume = value;
                break;
              case 'volume_up':
                v.volume = Math.min(1, v.volume + value);
                break;
              case 'volume_down':
                v.volume = Math.max(0, v.volume - value);
                break;
              case 'seek_to':
                v.currentTime = Math.min(
                  value, Number.isFinite(v.duration) ? v.duration : value
                );
                break;
              case 'seek_forward':
                v.currentTime = Math.min(
                  v.currentTime + value,
                  Number.isFinite(v.duration) ? v.duration : v.currentTime + value
                );
                break;
              case 'seek_backward':
                v.currentTime = Math.max(0, v.currentTime - value);
                break;
              case 'set_speed':
                v.playbackRate = value;
                break;
              case 'subtitles_on': {
                const t = [...v.textTracks].find(
                  x => x.kind === 'captions' || x.kind === 'subtitles'
                );
                if (!t) return {ok:false,error:'Дорожки субтитров не найдены'};
                t.mode = 'showing';
                break;
              }
              case 'subtitles_off':
                [...v.textTracks].forEach(t => {
                  if (t.mode === 'showing') t.mode = 'disabled';
                });
                break;
              case 'subtitles_toggle': {
                const tracks = [...v.textTracks].filter(
                  t => t.kind === 'captions' || t.kind === 'subtitles'
                );
                if (!tracks.length)
                  return {ok:false,error:'Дорожки субтитров не найдены'};
                tracks[0].mode = tracks.some(t => t.mode === 'showing')
                  ? 'disabled' : 'showing';
                break;
              }
              case 'fullscreen_enter':
                if (!document.fullscreenElement)
                  await v.requestFullscreen();
                break;
              case 'fullscreen_exit':
                if (document.fullscreenElement)
                  await document.exitFullscreen();
                break;
            }
            return {
              ok:true,
              paused:v.paused,
              currentTime:v.currentTime,
              volume:v.volume,
              muted:v.muted,
              playbackRate:v.playbackRate,
              fullscreen:!!document.fullscreenElement
            };
          } catch (e) {
            return {ok:false,error:String(e)};
          }
        }
        """, {"action": action, "value": value})

        if not result.get("ok"):
            return result

        return {"ok": True, "action": action, "state": result}