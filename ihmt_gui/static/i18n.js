/* Interface texts in Spanish and English.
 *
 * The backend never returns translatable sentences: only data. Everything read
 * on screen comes from here, except the contradiction notices, which the IHMT
 * core generates in English and are shown as is, labelled as such.
 */

const STRINGS = {
  es: {
    "app.title": "IHMT · Memoria",
    "app.subtitle": "Memoria jerárquica local para modelos de lenguaje",
    "nav.setup": "Configurar",
    "nav.explore": "Explorar",
    "nav.diagnose": "Diagnóstico",
    "nav.timeline": "Línea temporal",

    "common.folder": "Carpeta",
    "common.browse": "Examinar…",
    "common.use": "Usar esta carpeta",
    "common.copy": "Copiar",
    "common.copied": "Copiado",
    "common.apply": "Aplicar",
    "common.cancel": "Cancelar",
    "common.close": "Cerrar",
    "common.loading": "Cargando…",
    "common.empty": "No hay nada todavía.",
    "common.error": "Algo ha fallado",
    "common.retry": "Reintentar",
    "common.date": "Fecha",
    "common.domain": "Dominio",
    "common.tags": "Etiquetas",
    "common.source": "Origen",
    "common.path": "Ruta en el árbol",
    "common.leaves": "hojas",
    "common.nodes": "nodos de resumen",
    "common.depth": "profundidad",
    "common.facts": "hechos",
    "common.conflicts": "contradicciones",
    "common.yes": "Sí",
    "common.no": "No",

    "verdict.working": "Funcionando",
    "verdict.workingHelp": "Claude Code puede consultar y guardar en esta memoria.",
    "verdict.pending": "Falta un paso para que funcione",
    "verdict.pendingHelp": "Está registrado, pero Claude Code todavía no lo ha aprobado. Hasta que lo apruebes, no puede usar la memoria.",
    "verdict.notRegistered": "Sin registrar",
    "verdict.notRegisteredHelp": "Claude Code aún no conoce esta memoria. Elige un ámbito más abajo y pulsa Aplicar.",
    "verdict.sdkMissing": "Falta un paquete",
    "verdict.sdkMissingHelp": "El servidor no puede arrancar sin el SDK de MCP, aunque esté registrado.",
    "verdict.cliMissing": "No se encuentra Claude Code",
    "verdict.cliMissingHelp": "No hay ningún comando «claude» en este ordenador, así que no se puede comprobar ni registrar nada a nivel de usuario.",
    "verdict.failed": "Registrado, pero no arranca",
    "verdict.failedHelp": "Claude Code lo conoce pero no consigue conectarse. El detalle técnico de abajo dice por qué.",
    "verdict.unknown": "Estado desconocido",
    "verdict.unknownHelp": "Claude Code ha respondido algo que no sabemos interpretar. Abajo está su respuesta literal.",
    "verdict.recheck": "Volver a comprobar",
    "verdict.checking": "Comprobando…",
    "verdict.detail": "ver detalle técnico",
    "verdict.detailHide": "ocultar detalle técnico",
    "verdict.scopeProject": "Activo solo en este proyecto",
    "verdict.scopeUser": "Activo en todos tus proyectos",
    "verdict.scopeLocal": "Activo solo para ti en esta carpeta",
    "verdict.memoryHere": "La memoria está en {0}",
    "verdict.stepsTitle": "Qué tienes que hacer:",
    "verdict.stepTerminal": "Abre una terminal",
    "verdict.stepCd": "Ve a la carpeta:",
    "verdict.stepRun": "Ejecuta:",
    "verdict.stepApprove": "Apruébalo cuando te lo pregunte, y vuelve aquí",
    "verdict.stepInstallSdk": "Instala el SDK:",
    "verdict.stepInstallCli": "Instala Claude Code desde claude.com/claude-code",
    "verdict.noteDuplicate": "Hay también un registro en el .mcp.json de este proyecto, pero manda el otro.",
    "verdict.noteNoMemory": "Esa carpeta todavía no tiene memoria: se creará sola la primera vez que Claude Code la use.",
    "verdict.noteUserScope": "¿Se te resiste la aprobación? Los registros de proyecto (.mcp.json) la piden siempre, porque ese fichero podría venir de un repositorio ajeno. El ámbito «Todos los proyectos» no la necesita: cámbialo abajo y pulsa Aplicar.",
    "setup.heading": "Dónde vive tu memoria",
    "setup.intro":
      "Elige la carpeta donde se guardarán tus recuerdos. Es una carpeta normal: puedes moverla, copiarla o hacerle una copia de seguridad como cualquier otra.",
    "setup.exists": "Aquí ya hay una memoria",
    "setup.missing": "Aquí todavía no hay ninguna memoria",
    "setup.create": "Crear la memoria aquí",
    "setup.created": "Memoria creada",
    "setup.projectHeading": "¿En qué proyecto?",
    "setup.projectHelp": "La carpeta del proyecto desde el que quieres usar esta memoria. Ahí se escribirá el .mcp.json. No tiene por qué ser la misma carpeta donde viven los recuerdos.",
    "setup.memoryVsProject": "Son dos cosas distintas: arriba eliges DÓNDE se guardan los recuerdos; aquí, DESDE DÓNDE se pueden usar.",
    "setup.scopeHeading": "¿Para un proyecto o para todos?",
    "setup.scopeIntro":
      "Esto decide desde dónde podrá Claude Code consultar esta memoria. Puedes cambiarlo cuando quieras.",
    "setup.scopeProject": "Solo este proyecto",
    "setup.scopeProjectHelp":
      "Solo funciona cuando trabajas dentro de la carpeta que elijas abajo. Ideal para tener una memoria por cliente, por curso o por aplicación, sin que se mezclen entre sí.",
    "setup.scopeUser": "Todos los proyectos",
    "setup.scopeUserHelp":
      "Funciona en cualquier carpeta de tu ordenador, con una única memoria compartida. Es lo que casi siempre quieres: todo lo que le cuentes estará disponible siempre.",
    "setup.recommended": "recomendado",
    "setup.previewHeading": "Esto es exactamente lo que se hará",
    "setup.previewIntro": "No se toca nada hasta que pulses Aplicar.",
    "setup.willWrite": "Se escribirá el fichero",
    "setup.willRun": "Se ejecutará el comando",
    "setup.applied": "Configuración aplicada",
    "setup.restart":
      "Reinicia Claude Code para que tome efecto. La primera vez te pedirá aprobar el servidor.",
    "setup.problemNoCli":
      "No se encuentra el comando «claude». Instala Claude Code o usa el ámbito «solo este proyecto», que no lo necesita.",
    "setup.problemNoSdk":
      "Falta el SDK de MCP. Ejecuta: python3 -m venv .venv && .venv/bin/pip install -r requirements-mcp.txt",
    "setup.problemKeepsOthers": "Se conservarán los demás servidores ya presentes en el fichero.",
    "setup.problemReplaces": "Ya había un registro con este nombre: quedará sustituido.",
    "setup.pickerUnavailable":
      "El diálogo del sistema no está disponible. Usa el explorador de carpetas.",

    "explore.heading": "Qué hay guardado",
    "explore.intro":
      "El árbol baja de los dominios a las ramas de resumen y de ahí al texto que se guardó.",
    "explore.selectLeaf": "Elige una hoja del árbol para leerla.",
    "explore.superseded": "Esta información fue actualizada después",
    "explore.noticeOrigin": "aviso generado por IHMT (en inglés)",
    "explore.tokens": "tokens",
    "explore.lines": "líneas",
    "explore.context": "Contexto",
    "explore.truncated": "Texto recortado para mostrarlo aquí.",
    "explore.noMemory": "Primero crea o elige una memoria en la pestaña Configurar.",
    "explore.filterPlaceholder": "filtrar dominios",
    "explore.noDomains": "Ningún dominio coincide con «{0}».",

    "diagnose.heading": "Probar una búsqueda",
    "diagnose.intro":
      "Sirve para entender por qué una consulta encuentra o no encuentra algo. En el día a día no hace falta: se busca hablando con Claude Code.",
    "diagnose.placeholder": "por ejemplo: reserva de stock con bloqueo",
    "diagnose.cluePlaceholder": "pista para desambiguar",
    "diagnose.run": "Buscar",
    "diagnose.confidence": "confianza",
    "diagnose.opened": "ficheros abiertos",
    "diagnose.of": "de",
    "diagnose.ambiguous": "Consulta ambigua",
    "diagnose.ambiguousHelp":
      "Hay varias memorias que encajan igual de bien. IHMT no elige al azar: añade una pista y vuelve a buscar.",
    "diagnose.noResults": "No hay nada guardado sobre esto.",
    "diagnose.tryClue": "Buscar con esta pista",

    "timeline.heading": "Qué ha cambiado con el tiempo",
    "timeline.intro":
      "IHMT nunca borra: cuando algo se actualiza, el valor nuevo pasa a ser el activo y el anterior queda como histórico.",
    "timeline.active": "activo",
    "timeline.historical": "histórico",
    "timeline.noFacts": "Todavía no se ha extraído ningún hecho con fecha.",
    "timeline.conflictsHeading": "Contradicciones detectadas",
    "timeline.noConflicts": "No hay contradicciones.",
    "timeline.filterPlaceholder": "filtrar por sujeto o atributo",
    "timeline.showing": "Mostrando {0} de {1}",
    "timeline.loadMore": "Cargar más ({0} restantes)",
    "timeline.noMatches": "Ningún dato coincide con «{0}».",
    "timeline.origin": "origen",
    "timeline.showMiddle": "ver {0} valores intermedios",
    "timeline.hideMiddle": "ocultar los intermedios",
  },

  en: {
    "app.title": "IHMT · Memory",
    "app.subtitle": "Local hierarchical memory for language models",
    "nav.setup": "Set up",
    "nav.explore": "Explore",
    "nav.diagnose": "Diagnose",
    "nav.timeline": "Timeline",

    "common.folder": "Folder",
    "common.browse": "Browse…",
    "common.use": "Use this folder",
    "common.copy": "Copy",
    "common.copied": "Copied",
    "common.apply": "Apply",
    "common.cancel": "Cancel",
    "common.close": "Close",
    "common.loading": "Loading…",
    "common.empty": "Nothing here yet.",
    "common.error": "Something went wrong",
    "common.retry": "Retry",
    "common.date": "Date",
    "common.domain": "Domain",
    "common.tags": "Tags",
    "common.source": "Source",
    "common.path": "Path through the tree",
    "common.leaves": "leaves",
    "common.nodes": "summary nodes",
    "common.depth": "depth",
    "common.facts": "facts",
    "common.conflicts": "contradictions",
    "common.yes": "Yes",
    "common.no": "No",

    "verdict.working": "Working",
    "verdict.workingHelp": "Claude Code can read from and write to this memory.",
    "verdict.pending": "One step left",
    "verdict.pendingHelp": "It is registered, but Claude Code has not approved it yet. Until you approve it, it cannot use the memory.",
    "verdict.notRegistered": "Not registered",
    "verdict.notRegisteredHelp": "Claude Code does not know about this memory yet. Pick a scope below and press Apply.",
    "verdict.sdkMissing": "A package is missing",
    "verdict.sdkMissingHelp": "The server cannot start without the MCP SDK, even if it is registered.",
    "verdict.cliMissing": "Claude Code not found",
    "verdict.cliMissingHelp": "There is no 'claude' command on this machine, so nothing can be checked or registered at user level.",
    "verdict.failed": "Registered, but not starting",
    "verdict.failedHelp": "Claude Code knows about it but cannot connect. The technical detail below says why.",
    "verdict.unknown": "Unknown state",
    "verdict.unknownHelp": "Claude Code replied something we cannot interpret. Its literal answer is below.",
    "verdict.recheck": "Check again",
    "verdict.checking": "Checking…",
    "verdict.detail": "show technical detail",
    "verdict.detailHide": "hide technical detail",
    "verdict.scopeProject": "Active in this project only",
    "verdict.scopeUser": "Active across all your projects",
    "verdict.scopeLocal": "Active only for you in this folder",
    "verdict.memoryHere": "The memory lives in {0}",
    "verdict.stepsTitle": "What you need to do:",
    "verdict.stepTerminal": "Open a terminal",
    "verdict.stepCd": "Go to the folder:",
    "verdict.stepRun": "Run:",
    "verdict.stepApprove": "Approve it when asked, then come back here",
    "verdict.stepInstallSdk": "Install the SDK:",
    "verdict.stepInstallCli": "Install Claude Code from claude.com/claude-code",
    "verdict.noteDuplicate": "There is also an entry in this project's .mcp.json, but the other one takes precedence.",
    "verdict.noteNoMemory": "That folder has no memory yet: it will be created the first time Claude Code uses it.",
    "verdict.noteUserScope": "Approval not sticking? Project registrations (.mcp.json) always ask for it, because that file could come from someone else's repository. The “All projects” scope does not need it: switch below and press Apply.",
    "setup.heading": "Where your memory lives",
    "setup.intro":
      "Pick the folder that will hold your memories. It is an ordinary folder: move it, copy it or back it up like any other.",
    "setup.exists": "A memory already exists here",
    "setup.missing": "No memory here yet",
    "setup.create": "Create the memory here",
    "setup.created": "Memory created",
    "setup.projectHeading": "Which project?",
    "setup.projectHelp": "The project folder you want to use this memory from. The .mcp.json goes there. It does not have to be the same folder where the memories live.",
    "setup.memoryVsProject": "Two different things: above you pick WHERE the memories are stored; here, WHERE THEY CAN BE USED FROM.",
    "setup.scopeHeading": "One project, or all of them?",
    "setup.scopeIntro":
      "This decides where Claude Code can reach this memory from. You can change it whenever you like.",
    "setup.scopeProject": "This project only",
    "setup.scopeProjectHelp":
      "Only works while you are inside the folder you pick below. Ideal for one memory per client, per course or per app, with nothing bleeding between them.",
    "setup.scopeUser": "All projects",
    "setup.scopeUserHelp":
      "Works from any folder on your machine, with a single shared memory. This is what you usually want: everything you tell it is always available.",
    "setup.recommended": "recommended",
    "setup.previewHeading": "This is exactly what will happen",
    "setup.previewIntro": "Nothing is touched until you press Apply.",
    "setup.willWrite": "This file will be written",
    "setup.willRun": "This command will run",
    "setup.applied": "Configuration applied",
    "setup.restart":
      "Restart Claude Code for it to take effect. The first time it will ask you to approve the server.",
    "setup.problemNoCli":
      "The 'claude' command was not found. Install Claude Code, or use the 'this project only' scope, which does not need it.",
    "setup.problemNoSdk":
      "The MCP SDK is missing. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements-mcp.txt",
    "setup.problemKeepsOthers": "Any other servers already in the file will be kept.",
    "setup.problemReplaces": "An entry with this name already exists and will be replaced.",
    "setup.pickerUnavailable":
      "The system dialog is unavailable. Use the folder browser instead.",

    "explore.heading": "What is stored",
    "explore.intro":
      "The tree goes from domains down to summary branches, and from there to the text that was saved.",
    "explore.selectLeaf": "Pick a leaf from the tree to read it.",
    "explore.superseded": "This information was updated later",
    "explore.noticeOrigin": "notice generated by IHMT (in English)",
    "explore.tokens": "tokens",
    "explore.lines": "lines",
    "explore.context": "Context",
    "explore.truncated": "Text shortened for display.",
    "explore.noMemory": "Create or pick a memory in the Set up tab first.",
    "explore.filterPlaceholder": "filter domains",
    "explore.noDomains": "No domain matches “{0}”.",

    "diagnose.heading": "Try a search",
    "diagnose.intro":
      "This is for understanding why a query does or does not find something. Day to day you do not need it: you search by talking to Claude Code.",
    "diagnose.placeholder": "for example: stock reservation locking",
    "diagnose.cluePlaceholder": "clue to disambiguate",
    "diagnose.run": "Search",
    "diagnose.confidence": "confidence",
    "diagnose.opened": "files opened",
    "diagnose.of": "of",
    "diagnose.ambiguous": "Ambiguous query",
    "diagnose.ambiguousHelp":
      "Several memories match equally well. IHMT will not pick one at random: add a clue and search again.",
    "diagnose.noResults": "Nothing is stored about this.",
    "diagnose.tryClue": "Search with this clue",

    "timeline.heading": "What changed over time",
    "timeline.intro":
      "IHMT never deletes: when something is updated, the new value becomes active and the previous one is kept as history.",
    "timeline.active": "active",
    "timeline.historical": "historical",
    "timeline.noFacts": "No dated facts have been extracted yet.",
    "timeline.conflictsHeading": "Contradictions found",
    "timeline.noConflicts": "No contradictions.",
    "timeline.filterPlaceholder": "filter by subject or attribute",
    "timeline.showing": "Showing {0} of {1}",
    "timeline.loadMore": "Load more ({0} left)",
    "timeline.noMatches": "Nothing matches “{0}”.",
    "timeline.origin": "origin",
    "timeline.showMiddle": "show {0} values in between",
    "timeline.hideMiddle": "hide the ones in between",
  },
};

let currentLang = localStorage.getItem("ihmt.lang") || (navigator.language || "en").slice(0, 2);
if (!STRINGS[currentLang]) currentLang = "en";

/** Return a key's text, replacing {0}, {1}... with the arguments. */
function t(key, ...args) {
  const plantilla = STRINGS[currentLang][key] ?? STRINGS.en[key] ?? key;
  if (!args.length) return plantilla;
  return plantilla.replace(/\{(\d+)\}/g, (coincidencia, indice) => {
    const valor = args[Number(indice)];
    return valor === undefined ? coincidencia : String(valor);
  });
}

/** Change the language and redraw the static texts. */
function setLang(lang) {
  if (!STRINGS[lang]) return;
  currentLang = lang;
  localStorage.setItem("ihmt.lang", lang);
  document.documentElement.lang = lang;
  applyStaticStrings();
  document.dispatchEvent(new CustomEvent("ihmt:lang"));
}

/** Fill in everything marked with data-i18n / data-i18n-ph. */
function applyStaticStrings() {
  document.title = t("app.title");
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    el.textContent = t(el.dataset.i18n);
  });
  document.querySelectorAll("[data-i18n-ph]").forEach((el) => {
    el.placeholder = t(el.dataset.i18nPh);
  });
  document.querySelectorAll("[data-lang-btn]").forEach((el) => {
    el.classList.toggle("is-active", el.dataset.langBtn === currentLang);
  });
}

export { t, setLang, applyStaticStrings, currentLang as lang };
export function getLang() {
  return currentLang;
}
