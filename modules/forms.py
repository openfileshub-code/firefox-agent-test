"""Инвентаризация и заполнение видимых полей форм без обхода капчи."""

from __future__ import annotations

from typing import Any


FIELDS_JS = r"""
() => {
  const visible = el => {
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' &&
      Number(s.opacity) !== 0 && r.width > 0 && r.height > 0;
  };
  return [...document.querySelectorAll('input,textarea,select')]
    .slice(0, 300)
    .map((el, index) => ({
      index,
      tag: el.tagName.toLowerCase(),
      type: (el.type || '').toLowerCase(),
      name: el.name || '',
      id: el.id || '',
      labels: el.labels
        ? [...el.labels].map(x => x.innerText.trim()).join(' ')
        : '',
      placeholder: el.placeholder || '',
      required: !!el.required,
      visible: visible(el),
      enabled: !el.disabled &&
        el.getAttribute('aria-disabled') !== 'true',
      checked: !!el.checked,
      value: el.type === 'password' ? '' : (el.value || '').slice(0, 200)
    }));
}
"""

CAPTCHA_JS = r"""
() => {
  const selectors = [
    'iframe[src*="captcha" i]',
    'iframe[src*="recaptcha" i]',
    'iframe[src*="hcaptcha" i]',
    'iframe[src*="turnstile" i]',
    '[class*="captcha" i]',
    '[id*="captcha" i]',
    '[data-sitekey]'
  ];
  const matches = [...document.querySelectorAll(selectors.join(','))];
  const text = (document.body?.innerText || '').slice(0, 30000);
  const textHint =
    /captcha|verify you are human|я не робот|проверка на робота/i.test(text);
  return {
    detected: matches.length > 0 || textHint,
    elements: matches.slice(0, 20).map(el => ({
      tag: el.tagName.toLowerCase(),
      id: el.id || '',
      src: el.src || ''
    }))
  };
}
"""


class Forms:
    async def inventory(self, page: Any) -> dict:
        fields = await page.evaluate(FIELDS_JS)
        captcha = await page.evaluate(CAPTCHA_JS)
        return {"fields": fields, "captcha": captcha}

    async def fill(self, page: Any, values: dict[str, Any]) -> dict:
        if not isinstance(values, dict) or not values:
            raise ValueError("values должен быть непустым объектом")

        captcha = await page.evaluate(CAPTCHA_JS)
        if captcha.get("detected"):
            return {
                "filled": [],
                "skipped": [],
                "captcha_detected": True,
                "message": "Возможная капча: требуется ручное участие пользователя.",
            }

        fields = await page.evaluate(FIELDS_JS)
        by_key = {}

        for item in fields:
            for key in (
                item["name"], item["id"], item["labels"], item["placeholder"]
            ):
                if key:
                    by_key.setdefault(str(key).casefold(), item)

        filled = []
        skipped = []

        for key, value in values.items():
            field = by_key.get(str(key).casefold())
            if field is None:
                skipped.append({"field": key, "reason": "поле не найдено"})
                continue
            if not field["visible"] or not field["enabled"]:
                skipped.append({
                    "field": key,
                    "reason": "поле скрыто или отключено",
                })
                continue

            if field["id"]:
                locator = page.locator("#" + field["id"]).first
            elif field["name"]:
                locator = page.locator("input[name],textarea[name],select[name]").filter(
                    has=page.locator(
                        '[name="' + field["name"].replace('"', '\\"') + '"]'
                    )
                ).first
            else:
                locator = page.locator(
                    "input,textarea,select"
                ).nth(field["index"])

            try:
                if not await locator.is_visible() or not await locator.is_enabled():
                    skipped.append({"field": key, "reason": "состояние поля изменилось"})
                    continue

                if field["type"] in ("checkbox", "radio"):
                    if bool(value):
                        await locator.check()
                    else:
                        await locator.uncheck()
                elif field["tag"] == "select":
                    await locator.select_option(str(value))
                else:
                    await locator.fill(str(value))

                filled.append(str(key))
            except Exception as exc:
                skipped.append({"field": key, "reason": str(exc)[:240]})

        return {
            "filled": filled,
            "skipped": skipped,
            "captcha_detected": False,
            "submitted": False,
            "message": "Форма не отправлялась. Перед отправкой проверьте значения.",
        }