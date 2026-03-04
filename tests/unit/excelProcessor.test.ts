import {
    removeAccents,
    formatDataValue,
    normalizeText,
    parseNumber,
    parseNumberOrNull,
    parseStringOrNull,
    mapModalidad,
    parseOcupaciones
} from '../../excelProcessor';

describe('excelProcessor Helper Functions', () => {
    describe('mapModalidad', () => {
        it('should return mixta if only mixta is present', () => {
            expect(mapModalidad('Mixta')).toBe('mixta');
        });
        it('should return presencial if only presencial is present', () => {
            expect(mapModalidad('Sólo Presencial')).toBe('presencial');
        });
        it('should return teleformacion if only teleformacion is present', () => {
            expect(mapModalidad('Teleformación')).toBe('teleformacion');
        });
        it('should return mixta if both presencial and teleformacion are present', () => {
            expect(mapModalidad('Presencial y teleformación')).toBe('mixta');
            expect(mapModalidad('Teleformación o presencial mixta')).toBe('mixta');
        });
        it('should default to presencial', () => {
            expect(mapModalidad('Otra cosa')).toBe('presencial');
        });
    });

    describe('formatDataValue (para códigos)', () => {
        it('should normalize and replace non-alphanumeric with underscores', () => {
            expect(formatDataValue('IMSV0031')).toBe('imsv0031');
            expect(formatDataValue(' CÓDIGO-123 ')).toBe('codigo_123');
        });
    });

    describe('normalizeText (para valores de texto)', () => {
        it('should normalize text keeping spaces', () => {
            expect(normalizeText('DRONE EN LA PRODUCCIÓN AUDIOVISUAL')).toBe('drone en la produccion audiovisual');
            expect(normalizeText('Área de Informática y Comunicaciones')).toBe('area de informatica y comunicaciones');
            expect(normalizeText(' TÉCNICAS  básicas ')).toBe('tecnicas basicas');
        });
    });

    describe('parseOcupaciones', () => {
        it('should parse valid ocupaciones string', () => {
            const input = "31311142 - TÉCNICOS DE OPERACIÓN, 29341012 - AYUDANTES DE PROGRAMACIÓN";
            const result = parseOcupaciones(input);
            expect(result).toHaveLength(2);
            expect(result[0]).toEqual({ codigo: '31311142', descripcion: 'tecnicos de operacion' });
            expect(result[1]).toEqual({ codigo: '29341012', descripcion: 'ayudantes de programacion' });
        });
        it('should return empty array for "-" or empty', () => {
            expect(parseOcupaciones('-')).toEqual([]);
            expect(parseOcupaciones('   ')).toEqual([]);
        });
    });

    describe('Numbers and Strings parsing', () => {
        it('parseNumber should return 0 for invalid and parsed number for valid', () => {
            expect(parseNumber('123')).toBe(123);
            expect(parseNumber('-')).toBe(0);
        });
        it('parseNumberOrNull should return null for "-" and numbers otherwise', () => {
            expect(parseNumberOrNull('-')).toBeNull();
            expect(parseNumberOrNull('123')).toBe(123);
            expect(parseNumberOrNull('algo')).toBeNull();
        });
        it('parseStringOrNull should handle "-" as null and normalize text', () => {
            expect(parseStringOrNull('-')).toBeNull();
            expect(parseStringOrNull('Hola Mundo')).toBe('hola mundo');
        });
    });
});
