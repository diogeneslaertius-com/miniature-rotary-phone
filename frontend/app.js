const state = {
  map: null,
  config: null,
  destinationMarker: null, // маркер конкретного резолвленного адреса/ориентира
};

function extractAccessCode(fullName) {
  // Ищем "код домофона 4321" / "код: 4321" / "код 4321" в тексте адреса.
  // Пока это просто regex по строке, а не отдельное структурированное
  // поле от бэкенда — работает для демо-сценария, но для реальных
  // данных стоит вынести код в отдельное поле GeocodeCandidate.
  const match = fullName.match(/код\s*(?:домофона)?\s*:?\s*(\d{3,6})/i);
  return match ? match[1] : null;
}

function setDestinationMarker(lat, lon, label, accessCode) {
  // Рисуем свою точку поверх карты, а не полагаемся на встроенный POI-слой:
  // категорийный тумблер POI не умеет "покажи только этот один Magnum",
  // а свой маркер по координатам из geocode-ответа — умеет.
  if (state.destinationMarker) {
    state.destinationMarker.destroy();
  }
  const el = document.createElement("div");
  el.className = "marker-destination";
  if (accessCode) {
    el.classList.add("has-access");
    el.textContent = "🚪";
  }
  el.title = label;
  state.destinationMarker = new mapgl.HtmlMarker(state.map, {
    coordinates: [lon, lat],
    html: el,
  });
}

async function loadConfig() {
  const res = await fetch("/api/config");
  return res.json();
}

async function initMap() {
  state.config = await loadConfig();

  if (!state.config.mapKey) {
    console.warn(
      "DGIS_MAP_KEY не задан в backend/.env — карта не отрисуется. См. README."
    );
  }

  state.map = new mapgl.Map("map", {
    center: state.config.defaultCenter, // задаётся в backend/.env, см. README
    zoom: 12,
    key: state.config.mapKey,
    style: state.config.styleId || undefined,
  });

  loadPostamats();
}

async function loadPostamats() {
  const res = await fetch("/api/postamats");
  const geojson = await res.json();

  geojson.features.forEach((f) => {
    const [lon, lat] = f.geometry.coordinates;
    const el = document.createElement("div");
    el.className = "marker-postamat";
    el.title = f.properties.name;
    el.addEventListener("click", () => showPostamatInfo(f.properties));
    // eslint-disable-next-line no-new
    new mapgl.HtmlMarker(state.map, {
      coordinates: [lon, lat],
      html: el,
    });
  });
}

function showPostamatInfo(props) {
  const isFull = props.free_slots === 0;
  openSheet(`
    <h3>${props.name}</h3>
    <p>Провайдер: ${props.provider}</p>
    <p style="${isFull ? "color: var(--danger); font-weight: 700;" : ""}">
      Свободно мест: ${props.free_slots}/${props.total_slots}${isFull ? " — ПВЗ заполнен" : ""}
    </p>
  `);
}

function openSheet(html) {
  const sheet = document.getElementById("sheet");
  document.getElementById("sheet-content").innerHTML = html;
  sheet.hidden = false;
}

async function geocode(query) {
  const res = await fetch("/api/geocode", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query }),
  });
  return res.json();
}

async function reportHardwareStatus(status) {
  // Не критично, если это не долетит — ESP32-S3 просто увидит "idle"
  // при следующем опросе и погаснет. Поэтому без try/catch-паники.
  fetch("/api/hardware/status", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status }),
  }).catch(() => {});
}

function renderCandidates(query, data) {
  if (!data.candidates.length) {
    openSheet(
      `<h3>Ничего не нашли</h3><p>Попробуйте уточнить адрес или ориентир.</p>`
    );
    return;
  }

  // Сразу показываем маркер лучшего кандидата — не ждём клика.
  const best = data.candidates[0];
  const bestCode = extractAccessCode(best.full_name);
  setDestinationMarker(best.lat, best.lon, best.full_name, bestCode);
  state.map.setCenter([best.lon, best.lat]);
  state.map.setZoom(16);

  // Код на входе — первое, что видно, ещё до списка вариантов. Это и
  // есть "показать сразу калитку/шлагбаум", которого раньше не было.
  const accessCard = bestCode
    ? `<div class="access-card">
        <span class="access-card__icon">🚪</span>
        <div>
          <div class="access-card__label">Код на входе</div>
          <div class="access-card__code">${bestCode}</div>
        </div>
      </div>`
    : "";

  const rows = data.candidates
    .map(
      (c) => `
      <div class="candidate" data-lat="${c.lat}" data-lon="${c.lon}" data-name="${c.full_name}" data-code="${extractAccessCode(c.full_name) || ""}">
        <span class="candidate__name">${c.full_name}</span>
        <span class="candidate__confidence">${Math.round(c.confidence * 100)}%</span>
      </div>`
    )
    .join("");

  openSheet(`<h3>«${query}»</h3>${accessCard}<p>Похожие адреса:</p>${rows}`);

  document.querySelectorAll(".candidate").forEach((el) => {
    el.addEventListener("click", () => {
      const lat = parseFloat(el.dataset.lat);
      const lon = parseFloat(el.dataset.lon);
      setDestinationMarker(lat, lon, el.dataset.name, el.dataset.code || null);
      state.map.setCenter([lon, lat]);
      state.map.setZoom(16);
    });
  });
}

document.getElementById("search-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const query = document.getElementById("search-input").value.trim();
  if (!query) return;
  const data = await geocode(query);
  renderCandidates(query, data);
});

// --- Голосовой ввод ---

let mediaRecorder = null;
let chunks = [];
let recorderState = "idle"; // "idle" | "recording" | "processing"
let requestSeq = 0; // "билетик" каждой сессии записи — отсеиваем устаревшие ответы

// --- SiriWave: волна поверх поля ввода, реагирует на реальный голос ---

let siriWave = null;
let waveAudioCtx = null;
let waveAnalyser = null;
let waveRAF = null;

function initSiriWave() {
  const container = document.getElementById("siri-wave-container");
  siriWave = new SiriWave({
    container,
    width: container.offsetWidth || 260,
    height: container.offsetHeight || 40,
    style: "ios9",
    autostart: false,
    speed: 0.2,
    amplitude: 1,
  });
}

function showWave() {
  document.getElementById("siri-wave-container").classList.add("is-active");
  if (siriWave) siriWave.start();
}

function hideWave() {
  document.getElementById("siri-wave-container").classList.remove("is-active");
  // Даём догореть CSS-переходу (0.4s), потом останавливаем анимацию волны,
  // чтобы не крутилась вхолостую под невидимым слоем.
  setTimeout(() => siriWave && siriWave.stop(), 400);
}

function startWaveReactive(stream) {
  waveAudioCtx = new (window.AudioContext || window.webkitAudioContext)();
  const source = waveAudioCtx.createMediaStreamSource(stream);
  waveAnalyser = waveAudioCtx.createAnalyser();
  waveAnalyser.fftSize = 256;
  source.connect(waveAnalyser);
  const data = new Uint8Array(waveAnalyser.frequencyBinCount);

  const tick = () => {
    waveAnalyser.getByteFrequencyData(data);
    const avg = data.reduce((a, b) => a + b, 0) / data.length;
    // Нормализация подобрана "на глаз" под обычный телефонный микрофон —
    // подстройте делитель на репетиции под реальное железо демо-устройства.
    const amplitude = Math.min(2, 0.4 + avg / 40);
    if (siriWave) siriWave.setAmplitude(amplitude);
    waveRAF = requestAnimationFrame(tick);
  };
  tick();
}

function stopWaveReactive() {
  if (waveRAF) cancelAnimationFrame(waveRAF);
  waveRAF = null;
  if (waveAudioCtx) {
    waveAudioCtx.close();
    waveAudioCtx = null;
  }
}

function setMicVisual(uiState) {
  const micBtn = document.getElementById("mic-btn");
  micBtn.classList.toggle("is-recording", uiState === "recording");
  micBtn.classList.toggle("is-processing", uiState === "processing");
  micBtn.disabled = uiState === "processing";
}

async function startRecording() {
  // Идёт запись или ещё обрабатывается прошлая — новую не начинаем.
  // Это и есть тот самый "серый" во время транскрибации.
  if (recorderState !== "idle") return;

  recorderState = "recording";
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  mediaRecorder = new MediaRecorder(stream);
  chunks = [];

  mediaRecorder.ondataavailable = (e) => chunks.push(e.data);
  mediaRecorder.onstop = onRecordingStop;
  mediaRecorder.start();

  setMicVisual("recording");
  showWave();
  startWaveReactive(stream);
}

function stopRecording() {
  if (mediaRecorder && mediaRecorder.state !== "inactive") {
    mediaRecorder.stop();
  }
  // Поток микрофона сейчас остановится — гасим реактивный анализ, но
  // саму волну оставляем идти (в дефолтном автономном режиме) до тех
  // пор, пока не придёт результат распознавания — по ТЗ "затухает,
  // когда распознает", а не в момент отпускания кнопки.
  stopWaveReactive();
}

async function onRecordingStop() {
  recorderState = "processing";
  setMicVisual("processing");

  const myRequestId = ++requestSeq;
  const blob = new Blob(chunks, { type: "audio/webm" });
  const status = document.getElementById("voice-status");
  status.hidden = false;
  status.textContent = "Распознаём голос…";
  reportHardwareStatus("processing");

  const formData = new FormData();
  formData.append("file", blob, "voice.webm");

  try {
    const res = await fetch("/api/voice", { method: "POST", body: formData });
    const data = await res.json();

    // Если это не последняя стартовавшая запись — результат устарел,
    // не перетираем им экран (при исправном UI такого уже не случится,
    // но это дешёвая страховка на будущее, в т.ч. для железной кнопки).
    if (myRequestId !== requestSeq) return;

    status.textContent = `Распознано: «${data.text}»`;
    document.getElementById("search-input").value = data.text;
    const geoData = await geocode(data.text);
    if (myRequestId !== requestSeq) return;

    renderCandidates(data.text, geoData);
    reportHardwareStatus(geoData.candidates.length ? "success" : "error");
  } catch (err) {
    if (myRequestId === requestSeq) {
      status.textContent =
        "Не удалось распознать. Попробуйте ещё раз или введите адрес текстом.";
      reportHardwareStatus("error");
    }
  } finally {
    if (myRequestId === requestSeq) {
      recorderState = "idle";
      setMicVisual("idle");
      hideWave(); // затухает именно здесь — когда результат уже готов
      setTimeout(() => {
        status.hidden = true;
        reportHardwareStatus("idle");
      }, 4000);
    }
  }
}

const micBtn = document.getElementById("mic-btn");
micBtn.addEventListener("mousedown", startRecording);
micBtn.addEventListener("mouseup", stopRecording);
micBtn.addEventListener("mouseleave", stopRecording);
micBtn.addEventListener("touchstart", (e) => {
  e.preventDefault();
  startRecording();
});
micBtn.addEventListener("touchend", (e) => {
  e.preventDefault();
  stopRecording();
});

// --- Мои задачи: маршрут на сегодня, ETA, статус доставки ---

let stopMarkers = [];

async function loadOrders() {
  const res = await fetch("/api/orders");
  const data = await res.json();

  stopMarkers.forEach((m) => m.destroy());
  stopMarkers = [];

  data.stops.forEach((stop) => {
    const el = document.createElement("div");
    el.className = "marker-stop";
    if (stop.status === "delivered") {
      el.classList.add("is-delivered");
      el.textContent = "✓";
    } else if (stop.status === "failed") {
      el.classList.add("is-failed");
      el.textContent = String(stop.order);
    } else {
      el.textContent = String(stop.order);
    }
    const marker = new mapgl.HtmlMarker(state.map, {
      coordinates: [stop.lon, stop.lat],
      html: el,
    });
    stopMarkers.push(marker);
  });

  renderOrdersSheet(data.stops);
}

function renderOrdersSheet(stops) {
  const rows = stops
    .map((s) => {
      if (s.status === "delivered") {
        const thumb = s.proof_photo
          ? `<img class="order-row__proof-thumb" src="/proofs/${s.proof_photo}" alt="Фото у двери" />`
          : "";
        return `
        <div class="order-row is-done" data-id="${s.id}">
          <div class="order-row__main">
            <span class="order-row__num order-row__num--done">✓</span>
            <div>
              <div class="order-row__address">${s.address}</div>
              <div class="order-row__eta">Доставлено, фото-пруф сохранён</div>
            </div>
            ${thumb}
          </div>
        </div>`;
      }

      if (s.status === "failed") {
        return `
        <div class="order-row is-failed" data-id="${s.id}">
          <div class="order-row__main">
            <span class="order-row__num">${s.order}</span>
            <div>
              <div class="order-row__address">${s.address}</div>
              <div class="order-row__eta">Не получилось доставить</div>
            </div>
          </div>
        </div>`;
      }

      return `
      <div class="order-row" data-id="${s.id}">
        <div class="order-row__main">
          <span class="order-row__num">${s.order}</span>
          <div>
            <div class="order-row__address">${s.address}</div>
            <div class="order-row__eta">≈ ${s.eta_minutes} мин</div>
          </div>
        </div>
        <div class="order-row__actions">
          <button class="order-btn order-btn--ok" data-action="delivered">📷 Доставлено</button>
          <button class="order-btn order-btn--fail" data-action="failed">Не получилось</button>
        </div>
      </div>`;
    })
    .join("");

  openSheet(`<h3>Маршрут на сегодня</h3><p>${stops.length} точек, порядок по близости</p>${rows}`);

  document.querySelectorAll(".order-row").forEach((row) => {
    const okBtn = row.querySelector('[data-action="delivered"]');
    const failBtn = row.querySelector('[data-action="failed"]');

    if (okBtn) {
      okBtn.addEventListener("click", () => triggerProofCapture(row.dataset.id));
    }
    if (failBtn) {
      failBtn.addEventListener("click", async () => {
        await fetch(`/api/orders/${row.dataset.id}/status`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status: "failed" }),
        });
        loadOrders();
      });
    }
  });
}

function showProofConfirmation(photoDataUrl) {
  // Полноэкранный celebratory-момент вместо тихого обновления строки в
  // списке — именно тот "красивый" финал фото-пруфа, который раньше не
  // был доведён до конца, только эндпоинт на бэкенде.
  const overlay = document.createElement("div");
  overlay.className = "proof-overlay";
  overlay.innerHTML = `
    <div class="proof-overlay__card">
      <img src="${photoDataUrl}" class="proof-overlay__photo" alt="Фото у двери" />
      <div class="proof-overlay__check">✓</div>
      <div class="proof-overlay__title">Доставлено</div>
      <div class="proof-overlay__subtitle">Фото-пруф сохранён</div>
    </div>`;
  document.body.appendChild(overlay);

  requestAnimationFrame(() => overlay.classList.add("is-visible"));
  setTimeout(() => {
    overlay.classList.remove("is-visible");
    setTimeout(() => overlay.remove(), 300);
  }, 1800);
}

function triggerProofCapture(orderId) {
  // capture="environment" на телефоне открывает заднюю камеру напрямую —
  // это и есть "фото у двери за 1 клик". На десктопе без камеры браузер
  // просто откроет выбор файла — рабочий мок для репетиции за столом,
  // без отдельной DEMO_MODE-ветки для этой фичи.
  const input = document.createElement("input");
  input.type = "file";
  input.accept = "image/*";
  input.capture = "environment";

  input.addEventListener("change", async () => {
    const file = input.files[0];
    if (!file) return;

    // Локальный превью через FileReader — не ждём ответа сервера, чтобы
    // показать фото мгновенно, сервер при этом всё равно сохраняет
    // оригинал и проставляет статус параллельно.
    const reader = new FileReader();
    reader.onload = () => showProofConfirmation(reader.result);
    reader.readAsDataURL(file);

    const formData = new FormData();
    formData.append("photo", file, file.name || "proof.jpg");

    await fetch(`/api/orders/${orderId}/proof`, {
      method: "POST",
      body: formData,
    });

    loadOrders();
  });

  input.click();
}

document.getElementById("orders-btn").addEventListener("click", loadOrders);

// --- Demo Script: полный фейлсейф на случай отказа микрофона/сети/бэкенда ---
// Alt + цифра 6 (верхний ряд клавиатуры, НЕ Numpad). Полностью на
// клиенте — не делает ни одного сетевого запроса, поэтому сработает,
// даже если backend/2ГИС/Whisper на сцене легли целиком.

const DEMO_SCRIPT = {
  recognizedText: "ул. Аманжолова, 94/3, кв 5, код 4321",
  candidates: [
    {
      full_name: "ул. Аманжолова 94/3, кв 5 (код домофона 4321)",
      lat: 43.2401,
      lon: 76.9452,
      confidence: 0.97,
      source: "2gis+fuzzy",
    },
  ],
};

function runDemoScript() {
  if (recorderState !== "idle") return; // не мешаем реальной записи, если она вдруг идёт

  const status = document.getElementById("voice-status");
  status.hidden = false;
  status.textContent = "Распознаём голос…";

  setTimeout(() => {
    status.textContent = `Распознано: «${DEMO_SCRIPT.recognizedText}»`;
    document.getElementById("search-input").value = DEMO_SCRIPT.recognizedText;
    renderCandidates(DEMO_SCRIPT.recognizedText, { candidates: DEMO_SCRIPT.candidates });
    setTimeout(() => {
      status.hidden = true;
    }, 4000);
  }, 900); // небольшая пауза для правдоподобия, не мгновенно
}

document.addEventListener("keydown", (e) => {
  if (e.altKey && e.code === "Digit6") {
    e.preventDefault();
    runDemoScript();
  }
});

// --- Мост к железной кнопке курьера (ESP32-S3) ---

function connectHardwareSocket() {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${protocol}//${location.host}/api/ws/hardware`);

  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.event === "start_recording") {
      // Физическая кнопка нажата — стартуем запись без участия пальца
      // на экране. Фиксированное окно ~4 сек: курьер успевает сказать
      // адрес/команду, дальше пайплайн такой же, как при ручном клике.
      startRecording();
      setTimeout(stopRecording, 4000);
    }
  };

  ws.onclose = () => {
    // Переподключение — конференц-вайфай нестабилен, это не должно
    // ронять демо. См. README: лучше поднимать свою точку доступа.
    setTimeout(connectHardwareSocket, 2000);
  };
}

initSiriWave();
initMap();
connectHardwareSocket();
