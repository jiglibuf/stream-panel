-- Звук RedZone: играет только та карта, которая сейчас крупная.
--
-- OBS микширует звук всех источников сразу, независимо от сцены, поэтому «звук активной
-- карты» делается переключением mute при смене сцены:
--   • карта 1 (своя игра)  → звук системы включён, потоки друзей молчат;
--   • карта 2 / 3 (друзья) → звук системы молчит, звучит только поток этой карты;
--   • уход из сцен RedZone → потоки карт молчат, звук системы возвращается как был.
-- Микрофон не трогается никогда — голос идёт всегда.
--
-- Имена источников и сцен должны совпадать с конфигом модуля (sources/card/hub).
-- Выключить: снять галочку этого скрипта в OBS (Инструменты → Скрипты) — звук вернётся
-- к обычному (всё микшируется вместе).
--
-- Грабли Lua-API: геттер называется `obs_source_muted` (как в C), `obs_source_get_muted`
-- не существует; во время загрузки коллекции источники ещё не созданы — поэтому имена
-- функций ищем с запасным вариантом, отсутствие источника это не ошибка, плюс есть
-- сторожевой таймер: он раз в 3 секунды приводит mute к нужному виду, не полагаясь на
-- момент события.

local obs = obslua

local SYSTEM = "Звук системы"
local CARD_INPUT = { [2] = "RZ К2 хаб", [3] = "RZ К3 хаб" }
local FOCUS = {
  ["RZ3 · 1"] = 1, ["RZ3 · 2"] = 2, ["RZ3 · 3"] = 3,
  ["RZ · К1"] = 1, ["RZ · К2"] = 2, ["RZ · К3"] = 3,
}
local WATCHDOG_MS = 3000

local get_muted = obs.obs_source_muted or obs.obs_source_get_muted   -- оба имени встречались
local set_muted = obs.obs_source_set_muted
local get_by_name = obs.obs_get_source_by_name
local release = obs.obs_source_release

local system_mute_before = nil   -- каким был «Звук системы» до входа в RedZone

function script_description()
  return "RedZone: в эфир идёт звук только той карты, которая сейчас крупная.\n" ..
         "Карта 1 — своя игра (звук системы), карты 2-3 — потоки из хаба. Микрофон не трогается."
end

local function set_mute(name, muted)
  if set_muted == nil or get_by_name == nil then return end
  local src = get_by_name(name)
  if src == nil then return end
  set_muted(src, muted)
  if release then release(src) end
end

local function system_muted()
  if get_muted == nil or get_by_name == nil then return nil end
  local src = get_by_name(SYSTEM)
  if src == nil then return nil end
  local m = get_muted(src)
  if release then release(src) end
  return m
end

local function apply()
  local src = obs.obs_frontend_get_current_scene()
  if src == nil then return end
  local name = obs.obs_source_get_name(src) or ""
  obs.obs_source_release(src)

  local focus = FOCUS[name]
  if focus == nil then
    set_mute(SYSTEM, system_mute_before or false)     -- обычная сцена: как было
  else
    if system_mute_before == nil then system_mute_before = system_muted() or false end
    set_mute(SYSTEM, focus ~= 1)
  end
  for n, input in pairs(CARD_INPUT) do
    set_mute(input, focus ~= n)
  end
end

function rz_on_event(event)
  if event == obs.OBS_FRONTEND_EVENT_SCENE_CHANGED
     or event == obs.OBS_FRONTEND_EVENT_FINISHED_LOADING
     or event == obs.OBS_FRONTEND_EVENT_SCENE_COLLECTION_CHANGED then
    apply()
  end
end

function script_load(settings)
  system_mute_before = system_muted()
  obs.obs_frontend_add_event_callback(rz_on_event)
  obs.timer_add(apply, WATCHDOG_MS)
  apply()
end

function script_unload()
  obs.timer_remove(apply)
  obs.obs_frontend_remove_event_callback(rz_on_event)
  set_mute(SYSTEM, system_mute_before or false)
  for _, input in pairs(CARD_INPUT) do set_mute(input, true) end
end
