const linkForm = document.querySelector("#link-form");
const sheetForm = document.querySelector("#sheet-form");
const urlInput = document.querySelector("#url");
const fileInput = document.querySelector("#sheet");
const fileName = document.querySelector("#file-name");
const drop = document.querySelector("#drop");
const note = document.querySelector("#ledger-note");
const detail = document.querySelector("#ledger-detail");
const rows = document.querySelector("#rows");
const buttons = document.querySelectorAll("button");

let timer = 0;
let watch = 0;
let chosenFile = null;

fileInput.addEventListener("change", () => {
  chosenFile = fileInput.files && fileInput.files[0] ? fileInput.files[0] : null;
  fileName.textContent = chosenFile ? chosenFile.name : "Drop a .csv here, or choose a file";
});

["dragenter", "dragover"].forEach((name) => {
  drop.addEventListener(name, (event) => {
    event.preventDefault();
    drop.classList.add("is-over");
  });
});

["dragleave", "drop"].forEach((name) => {
  drop.addEventListener(name, (event) => {
    event.preventDefault();
    drop.classList.remove("is-over");
  });
});

drop.addEventListener("drop", (event) => {
  const file = event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0];
  if (!file) return;
  chosenFile = file;
  fileName.textContent = file.name;
});

linkForm.addEventListener("submit", (event) => {
  event.preventDefault();
  start({ kind: "url", url: urlInput.value.trim() });
});

sheetForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!chosenFile) {
    showError("Choose a .csv file first.");
    return;
  }
  if (!chosenFile.name.toLowerCase().endsWith(".csv")) {
    showError("Use a .csv file.");
    return;
  }
  const csv = await chosenFile.text();
  start({ kind: "csv", filename: chosenFile.name, csv });
});

async function start(body) {
  setBusy(true);
  showLoading("Starting the read.");
  try {
    const response = await fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await response.json();
    if (!response.ok) {
      setBusy(false);
      showError(data.error || "The read did not start.");
      return;
    }
    poll(data.id);
  } catch (error) {
    setBusy(false);
    showError("The desk could not be reached.");
  }
}

function poll(id) {
  window.clearInterval(timer);
  const token = ++watch;
  const look = async () => {
    if (token !== watch) return;
    try {
      const response = await fetch("/api/jobs/" + id);
      const data = await response.json();
      if (token !== watch) return;
      if (!response.ok) {
        window.clearInterval(timer);
        setBusy(false);
        showError(data.error || "That read could not be found.");
        return;
      }
      if (data.status === "running") {
        showLoading(data.message);
        return;
      }
      window.clearInterval(timer);
      setBusy(false);
      if (data.status === "done") showDone(data);
      else showError(data.message);
    } catch (error) {
      window.clearInterval(timer);
      setBusy(false);
      showError("The desk stopped answering.");
    }
  };
  look();
  timer = window.setInterval(look, 1000);
}

function setBusy(on) {
  buttons.forEach((button) => {
    button.disabled = on;
  });
}

function showLoading(message) {
  note.textContent = message;
  detail.replaceChildren();
  rows.replaceChildren();
  for (let index = 0; index < 4; index += 1) {
    const row = document.createElement("tr");
    row.className = "skeleton";
    const cell = document.createElement("td");
    cell.colSpan = 6;
    const bar = document.createElement("span");
    bar.className = "bar-ghost";
    cell.append(bar);
    row.append(cell);
    rows.append(row);
  }
}

function showError(message) {
  note.textContent = "";
  detail.replaceChildren();
  const line = document.createElement("span");
  line.className = "error-line";
  const icon = document.createElement("img");
  icon.src = "/icons/alert.svg";
  icon.width = 24;
  icon.height = 24;
  icon.alt = "";
  line.append(icon, document.createTextNode(message));
  note.append(line);
  rows.replaceChildren();
  const row = document.createElement("tr");
  row.className = "quiet";
  const cell = document.createElement("td");
  cell.colSpan = 6;
  cell.textContent = "Nothing was written.";
  row.append(cell);
  rows.append(row);
}

function showDone(data) {
  note.textContent = data.count + (data.count === 1 ? " record" : " records");
  detail.textContent = data.message;
  const link = document.createElement("a");
  link.className = "download";
  link.href = data.download;
  const icon = document.createElement("img");
  icon.src = "/icons/download-on.svg";
  icon.width = 24;
  icon.height = 24;
  icon.alt = "";
  link.append(icon, document.createTextNode("Download CSV"));
  detail.append(document.createElement("br"), link);

  rows.replaceChildren();
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  data.preview.forEach((record) => {
    const row = document.createElement("tr");
    row.append(
      titleCell(record),
      textCell(record.dc_creator),
      textCell(record.dc_date),
      textCell(record.dc_type),
      textCell(record.dc_subject),
      textCell(record.dcterms_spatial),
    );
    rows.append(row);
  });
  if (!reduce && window.gsap) {
    window.gsap.from(rows.children, {
      opacity: 0,
      y: 8,
      duration: 0.22,
      stagger: 0.04,
      ease: "power3.out",
    });
  }
}

function titleCell(record) {
  const cell = document.createElement("td");
  const identifier = record.dc_identifier || "";
  if (identifier.startsWith("http://") || identifier.startsWith("https://")) {
    const link = document.createElement("a");
    link.href = identifier;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = record.dc_title || identifier;
    cell.append(link);
  } else {
    cell.textContent = record.dc_title || "";
  }
  return cell;
}

function textCell(value) {
  const cell = document.createElement("td");
  cell.textContent = value || "";
  return cell;
}
