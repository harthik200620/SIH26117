// Read-only validation with a separate spreadsheet calculation engine.
import fs from 'node:fs/promises';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { FileBlob, SpreadsheetFile } from '@oai/artifact-tool';

const [input, output, range = 'A1:P12'] = process.argv.slice(2);
if (!input || !output) throw new Error('Usage: node recalculate-workbook.mjs input.xlsx output-dir [range]');
const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(input));
workbook.recalculate();
const sheet = workbook.worksheets.getItemAt(0);
await fs.mkdir(output, { recursive: true });
await fs.writeFile(path.join(output, 'engine-record.json'), JSON.stringify({
  inputSha256: createHash('sha256').update(await fs.readFile(input)).digest('hex'),
  engine: '@oai/artifact-tool', inspectedRange: range, observedUtc: new Date().toISOString(),
}, null, 2));
await fs.writeFile(path.join(output, 'recalculated-values.json'), JSON.stringify(sheet.getRange(range).values, null, 2));
await fs.writeFile(path.join(output, 'formulas.json'), JSON.stringify(sheet.getRange(range).formulas, null, 2));
const errors = await workbook.inspect({
  kind: 'match', searchTerm: '#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!',
  options: { useRegex: true, maxResults: 100 }, maxChars: 4000,
});
await fs.writeFile(path.join(output, 'errors.ndjson'), errors.ndjson);
const overview = await workbook.inspect({kind: 'sheet', include: 'id,name', maxChars: 4000});
await fs.writeFile(path.join(output, 'sheets.ndjson'), overview.ndjson);
const preview = await workbook.render({sheetName: sheet.name, range, scale: 1, format: 'png'});
await fs.writeFile(path.join(output, 'workbook.png'), new Uint8Array(await preview.arrayBuffer()));
console.log(JSON.stringify({input, output, inspectedRange: range, engine: '@oai/artifact-tool', sourceUnmodified: true}));
