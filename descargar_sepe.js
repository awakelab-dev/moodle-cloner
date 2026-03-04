"use strict";
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
var __awaiter = (this && this.__awaiter) || function (thisArg, _arguments, P, generator) {
    function adopt(value) { return value instanceof P ? value : new P(function (resolve) { resolve(value); }); }
    return new (P || (P = Promise))(function (resolve, reject) {
        function fulfilled(value) { try { step(generator.next(value)); } catch (e) { reject(e); } }
        function rejected(value) { try { step(generator["throw"](value)); } catch (e) { reject(e); } }
        function step(result) { result.done ? resolve(result.value) : adopt(result.value).then(fulfilled, rejected); }
        step((generator = generator.apply(thisArg, _arguments || [])).next());
    });
};
var __generator = (this && this.__generator) || function (thisArg, body) {
    var _ = { label: 0, sent: function() { if (t[0] & 1) throw t[1]; return t[1]; }, trys: [], ops: [] }, f, y, t, g = Object.create((typeof Iterator === "function" ? Iterator : Object).prototype);
    return g.next = verb(0), g["throw"] = verb(1), g["return"] = verb(2), typeof Symbol === "function" && (g[Symbol.iterator] = function() { return this; }), g;
    function verb(n) { return function (v) { return step([n, v]); }; }
    function step(op) {
        if (f) throw new TypeError("Generator is already executing.");
        while (g && (g = 0, op[0] && (_ = 0)), _) try {
            if (f = 1, y && (t = op[0] & 2 ? y["return"] : op[0] ? y["throw"] || ((t = y["return"]) && t.call(y), 0) : y.next) && !(t = t.call(y, op[1])).done) return t;
            if (y = 0, t) op = [op[0] & 2, t.value];
            switch (op[0]) {
                case 0: case 1: t = op; break;
                case 4: _.label++; return { value: op[1], done: false };
                case 5: _.label++; y = op[1]; op = [0]; continue;
                case 7: op = _.ops.pop(); _.trys.pop(); continue;
                default:
                    if (!(t = _.trys, t = t.length > 0 && t[t.length - 1]) && (op[0] === 6 || op[0] === 2)) { _ = 0; continue; }
                    if (op[0] === 3 && (!t || (op[1] > t[0] && op[1] < t[3]))) { _.label = op[1]; break; }
                    if (op[0] === 6 && _.label < t[1]) { _.label = t[1]; t = op; break; }
                    if (t && _.label < t[2]) { _.label = t[2]; _.ops.push(op); break; }
                    if (t[2]) _.ops.pop();
                    _.trys.pop(); continue;
            }
            op = body.call(thisArg, _);
        } catch (e) { op = [6, e]; y = 0; } finally { f = t = 0; }
        if (op[0] & 5) throw op[1]; return { value: op[0] ? op[1] : void 0, done: true };
    }
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.downloadSepeFile = downloadSepeFile;
var playwright_1 = require("playwright");
var path = __importStar(require("path"));
var fs = __importStar(require("fs"));
/**
 * Función principal para automatizar la descarga desde la Sede del SEPE.
 * @returns {Promise<string | null>} La ruta absoluta del archivo descargado, o null si falla.
 */
function downloadSepeFile() {
    return __awaiter(this, void 0, void 0, function () {
        var downloadDir, EXTENDED_TIMEOUT, browser, context, page, selectorBtnBuscar, selectorTablaResultados, selectorBtnExportar, selectorModal, selectorCheckbox1, selectorCheckbox2, selectorCheckbox3, selectorBtnContinuar, downloadPromise, download, suggestedFilename, finalFilePath, error_1;
        return __generator(this, function (_a) {
            switch (_a.label) {
                case 0:
                    downloadDir = path.resolve(__dirname, 'descargas');
                    // Crear el directorio /descargas si no existe
                    if (!fs.existsSync(downloadDir)) {
                        fs.mkdirSync(downloadDir, { recursive: true });
                    }
                    EXTENDED_TIMEOUT = 60000;
                    return [4 /*yield*/, playwright_1.chromium.launch({ headless: true })];
                case 1:
                    browser = _a.sent();
                    return [4 /*yield*/, browser.newContext({
                            acceptDownloads: true
                        })];
                case 2:
                    context = _a.sent();
                    return [4 /*yield*/, context.newPage()];
                case 3:
                    page = _a.sent();
                    // Configurar timeouts por defecto en la página para evitar que falle en cargas lentas
                    page.setDefaultTimeout(EXTENDED_TIMEOUT);
                    page.setDefaultNavigationTimeout(EXTENDED_TIMEOUT);
                    _a.label = 4;
                case 4:
                    _a.trys.push([4, 16, 17, 19]);
                    console.log('1. Navegando a la página del SEPE...');
                    return [4 /*yield*/, page.goto('https://sede.sepe.gob.es/FOET_CATALOGO_EEFF_SEDE/flows/main?execution=e1s1')];
                case 5:
                    _a.sent();
                    console.log('2. Buscando el botón de BUSCAR y haciendo clic...');
                    selectorBtnBuscar = '#formulario\\:bBuscar';
                    return [4 /*yield*/, page.click(selectorBtnBuscar)];
                case 6:
                    _a.sent();
                    console.log('3. Esperando a que los resultados se carguen por completo...');
                    selectorTablaResultados = '#formGeneral\\:resultadoEspecialidadesTablaJSF';
                    return [4 /*yield*/, page.waitForSelector(selectorTablaResultados, { state: 'visible', timeout: EXTENDED_TIMEOUT })];
                case 7:
                    _a.sent();
                    console.log('4. Haciendo clic al botón "Exportar"...');
                    selectorBtnExportar = '#exportar';
                    return [4 /*yield*/, page.click(selectorBtnExportar)];
                case 8:
                    _a.sent();
                    console.log('5. Esperando a que aparezca la ventana modal...');
                    selectorModal = '#formulario\\:modalExportar';
                    return [4 /*yield*/, page.waitForSelector(selectorModal, { state: 'visible' })];
                case 9:
                    _a.sent();
                    console.log('6. Marcando los checkboxes dentro de la modal...');
                    selectorCheckbox1 = '#formulario\\:modalExportar\\:checkboxExportar\\:checkboxExportarSelect\\:0';
                    selectorCheckbox2 = '#formulario\\:modalExportar\\:checkboxExportar\\:checkboxExportarSelect\\:1';
                    selectorCheckbox3 = '#formulario\\:modalExportar\\:checkboxExportar\\:checkboxExportarSelect\\:2';
                    // page.check() marca la casilla si no está marcada, esperando que sea clickeable.
                    return [4 /*yield*/, page.check(selectorCheckbox1)];
                case 10:
                    // page.check() marca la casilla si no está marcada, esperando que sea clickeable.
                    _a.sent();
                    return [4 /*yield*/, page.check(selectorCheckbox2)];
                case 11:
                    _a.sent();
                    return [4 /*yield*/, page.check(selectorCheckbox3)];
                case 12:
                    _a.sent();
                    console.log('7. Preparando intercepción de la descarga y haciendo clic en Continuar...');
                    selectorBtnContinuar = '#formulario\\:modalExportar\\:botonImprimir';
                    downloadPromise = page.waitForEvent('download', { timeout: EXTENDED_TIMEOUT });
                    return [4 /*yield*/, page.click(selectorBtnContinuar)];
                case 13:
                    _a.sent();
                    console.log('8. Esperando a que finalice la descarga del archivo...');
                    return [4 /*yield*/, downloadPromise];
                case 14:
                    download = _a.sent();
                    suggestedFilename = download.suggestedFilename();
                    finalFilePath = path.join(downloadDir, suggestedFilename);
                    // Guardar el archivo desde el stream temporal hasta nuestra ruta final.
                    // El await garantiza que se complete la escritura en el disco.
                    return [4 /*yield*/, download.saveAs(finalFilePath)];
                case 15:
                    // Guardar el archivo desde el stream temporal hasta nuestra ruta final.
                    // El await garantiza que se complete la escritura en el disco.
                    _a.sent();
                    console.log("\u2705 Descarga completada con \u00E9xito. Archivo guardado en: ".concat(finalFilePath));
                    return [2 /*return*/, finalFilePath];
                case 16:
                    error_1 = _a.sent();
                    console.error('❌ Ocurrió un error en la automatización:', error_1);
                    return [2 /*return*/, null]; // O relanzar el error dependiendo del caso de uso.
                case 17:
                    console.log('9. Cerrando el navegador de forma segura...');
                    // Siempre cerrar el navegador para evitar procesos "zombis"
                    return [4 /*yield*/, browser.close()];
                case 18:
                    // Siempre cerrar el navegador para evitar procesos "zombis"
                    _a.sent();
                    return [7 /*endfinally*/];
                case 19: return [2 /*return*/];
            }
        });
    });
}
// Ejecución del script solo si se llama directamente
if (require.main === module) {
    downloadSepeFile().then(function (resultado) {
        if (resultado) {
            console.log('\n--- PROCESO FINALIZADO ---');
            console.log('Ruta final:', resultado);
        }
        else {
            console.log('\n--- PROCESO FINALIZADO CON ERRORES ---');
        }
    });
}
