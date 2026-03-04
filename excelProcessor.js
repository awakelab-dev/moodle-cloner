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
exports.processLatestExcel = void 0;
var fs = __importStar(require("fs"));
var path = __importStar(require("path"));
var xlsx = __importStar(require("xlsx"));
var Record_1 = require("./models/Record"); // The Mongoose Model
var processLatestExcel = function (downloadDir) { return __awaiter(void 0, void 0, void 0, function () {
    var files, excelFiles, latestFile, filePath, workbook, firstSheetName, worksheet, jsonData, error_1;
    return __generator(this, function (_a) {
        switch (_a.label) {
            case 0:
                _a.trys.push([0, 3, , 4]);
                console.log("Buscando archivos en: ".concat(downloadDir));
                files = fs.readdirSync(downloadDir);
                excelFiles = files.filter(function (f) { return f.endsWith('.xlsx'); })
                    .map(function (f) { return ({
                    name: f,
                    time: fs.statSync(path.join(downloadDir, f)).mtime.getTime()
                }); })
                    .sort(function (a, b) { return b.time - a.time; });
                if (excelFiles.length === 0) {
                    throw new Error('No se encontraron archivos Excel en el directorio.');
                }
                latestFile = excelFiles[0].name;
                filePath = path.join(downloadDir, latestFile);
                console.log("Procesando el archivo m\u00E1s reciente: ".concat(filePath));
                workbook = xlsx.readFile(filePath);
                firstSheetName = workbook.SheetNames[0];
                worksheet = workbook.Sheets[firstSheetName];
                jsonData = xlsx.utils.sheet_to_json(worksheet, { defval: "" });
                console.log("Se han extra\u00EDdo ".concat(jsonData.length, " filas. Insertando en MongoDB..."));
                // Limpiamos la colección antes de la inserción si el objetivo es tener solo la información actual 
                // Si queremos guardar el historico, comentamos esta linea. Voy a asumir que borramos para mantenerlo limpio
                return [4 /*yield*/, Record_1.Record.deleteMany({})];
            case 1:
                // Limpiamos la colección antes de la inserción si el objetivo es tener solo la información actual 
                // Si queremos guardar el historico, comentamos esta linea. Voy a asumir que borramos para mantenerlo limpio
                _a.sent();
                // Insertando todos los datos
                return [4 /*yield*/, Record_1.Record.insertMany(jsonData)];
            case 2:
                // Insertando todos los datos
                _a.sent();
                console.log('✅ Inserción en MongoDB completada exitosamente.');
                // Devolvemos los primeros 5 registros
                return [2 /*return*/, jsonData.slice(0, 5)];
            case 3:
                error_1 = _a.sent();
                console.error('❌ Error al procesar el Excel:', error_1);
                throw error_1;
            case 4: return [2 /*return*/];
        }
    });
}); };
exports.processLatestExcel = processLatestExcel;
