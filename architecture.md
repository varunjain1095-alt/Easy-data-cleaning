# Quick Data Cleaner — Architecture

## Product purpose

Quick Data Cleaner turns recurring, manually coded Python data-cleaning workflows into a reusable tool for preparing datasets for analysis.

## 1. Data ingestion

Version 1 uses file-based ingestion:

- File upload: CSV, XLS, or XLSX

Live database connections are excluded from Version 1. Users working with database data export the required table or query result to a supported file, upload it, and download the cleaned result as a file. The tool does not host a database or write changes back to a source database.

### 1.1 CSV parsing

- Automatically detect common encodings and comma, semicolon, tab, or pipe delimiters.
- Show the detected settings with the initial preview.
- Allow the user to correct the encoding or delimiter when the preview is incorrect.

### 1.2 Excel workbooks with multiple sheets or tables

When an Excel workbook contains multiple sheets or detectable tables, show a checklist of all available items. The user cleans one sheet or table at a time and can return to the checklist to start, resume, or review another.

Each sheet or table has its own status:

- Not started
- In progress
- Completed
- Completed with warnings
- Skipped

Cleaning state and transformation history are maintained separately for each item. Export recreates a cleaned workbook containing every processed sheet or table while retaining untouched sheets unchanged unless the user explicitly excludes them.

## 2. Initial data preview

After the data source is loaded successfully, the tool displays a simple preview before the cleaning workflow begins.

The preview includes:

- First few rows
- Total row count
- Total column count
- Column names
- Detected data types

This stage is intentionally limited to confirming that the correct data was loaded. It does not perform cleaning, diagnostics, recommendations, or transformations.

## Architecture status

The ingestion, initial-preview, missingness, intended data-type inference, normalization, duplicate detection, foundational cleaning, contextual outlier analysis, final validation, and export requirements have been defined. Other advanced workflows and technical implementation decisions will be added after they are discussed and explicitly approved for inclusion.

## 3. Missingness workflow

Missingness is the first cleaning workflow. The system first recognizes missing values in the raw data, then infers each column's intended type from its non-missing values. Actual type conversion happens only after missing and invalid values have been reviewed.

### 3.1 Column assessment

For every column, show:

- Missing-value count and percentage
- Missing representations detected, including actual nulls, blank strings, whitespace, and common markers such as `NA`, `N/A`, and `null`
- Detected or suggested data type
- Example rows affected
- Available treatment methods
- Recommended treatment with a short explanation

### 3.2 Supported treatments

The user can choose to:

- Leave missing values unchanged
- Remove affected rows
- Remove the column
- Replace with a user-defined constant
- Impute with the mean
- Impute with the median
- Impute with the mode
- Forward fill
- Backward fill
- Apply linear interpolation
- Apply time-based interpolation
- Apply group-based imputation using a selected grouping column and statistic
- Replace with a randomly sampled observed value

### 3.3 Availability by data type

- Numeric: mean, median, mode, constant, interpolation, group-based imputation, or random observed value
- Categorical: mode, constant, group-based mode, or random observed value
- Text: constant placeholder or empty string
- Datetime: forward fill, backward fill, constant date, or interpolation when meaningful ordering exists
- Boolean: mode, constant, or group-based mode

Only methods valid for the inferred data type and dataset structure are enabled.

### 3.4 Group-based imputation

Group-based imputation is a primary capability. Users select one or more grouping columns and a compatible statistic. For example, missing salary values may be filled using the median salary within each department and job level rather than the dataset-wide median.

The tool reports any groups that contain no usable observed value and therefore cannot be imputed using the selected method.

### 3.5 Recommendations and safeguards

- Recommendations remain optional; the user makes the final decision.
- Mean imputation warns when a numeric distribution is strongly skewed or contains influential outliers.
- Interpolation is enabled only when a meaningful ordering exists.
- Forward and backward fill require the user to confirm the column that defines row order.
- Row deletion previews the resulting row count and percentage of data removed.
- Column deletion warns about information loss.
- User-defined constants are validated against the intended data type.
- Every method shows affected-row count and a before-and-after preview before application.
- Every applied treatment is recorded and can be undone.

### 3.6 MVP boundary

The MVP includes deterministic missing-value treatments. Predictive imputation methods, including KNN, regression-based imputation, and MICE, are deferred to a later advanced version because they require additional controls for leakage, model quality, computation, and reproducibility.

## 4. Intended data-type inference

After missing-value representations have been recognized, the tool infers each column's intended data type using only its non-missing values. Type conversion is not applied automatically.

### 4.1 Supported types

- Integer
- Decimal or float
- Boolean
- String or text
- Categorical
- Date
- Datetime

### 4.2 Column assessment

For every column, show:

- Current detected type
- Suggested intended type
- Percentage of non-missing values compatible with the suggested type
- Number of incompatible non-missing values
- Examples of incompatible values
- Option to accept the suggestion, retain the current type, or manually choose another type

### 4.3 Safeguards

- Recognized missing values are excluded from type inference.
- No suggested type is applied without user confirmation.
- Numeric-looking identifiers such as ZIP codes, phone numbers, SKUs, account numbers, and other IDs remain strings when identifier characteristics are detected.
- Categorical suggestions consider the number and proportion of unique values rather than relying only on the stored type.
- Ambiguous date formats require the user to choose the intended interpretation, such as day-first or month-first.
- Before conversion, the tool previews values that would fail conversion or become null.
- The user can retain the existing type for any column.

After the user confirms the intended schema, the approved type conversions are applied and recorded in the transformation history.

## 5. Value normalization

After intended data types have been reviewed and confirmed, the tool normalizes values before later cleaning stages. Normalization is configured at the column level and is never applied without user confirmation.

### 5.1 Supported operations

- Trim leading and trailing whitespace
- Replace repeated internal spaces with a single space
- Convert text to lowercase
- Convert text to uppercase
- Convert text to title case
- Standardize empty strings as missing values
- Remove or replace line breaks and tabs
- Standardize common punctuation variations
- Map inconsistent categorical labels to a canonical value through find-and-replace or explicit value mapping

Example categorical mapping:

```text
"M", "Male", "male" -> "Male"
"F", "Female", "female" -> "Female"
```

### 5.2 Interaction model

The user can:

- Select one or multiple compatible columns
- Choose one or more normalization operations
- See the number and percentage of values affected
- Preview original and normalized values side by side
- Apply or reject each proposed transformation
- Undo an applied transformation

### 5.3 Safety classification

Normalization operations are separated into two classes:

- Safe normalization: whitespace trimming, repeated-space cleanup, and line-break or tab cleanup
- Meaning-changing normalization: case conversion, punctuation changes, and categorical value mapping

Safe normalization may be strongly recommended. Meaning-changing normalization always requires explicit user selection and confirmation.

The original values remain recoverable through transformation history, and every applied normalization step is included in the reproducible export.

## 6. Duplicate detection

Duplicate detection runs after normalization so superficial formatting differences do not conceal matching records. The tool does not remove or merge records automatically.

### 6.1 Comparison configuration

The user selects the columns that define a duplicate. All compatible columns may be selected by default, but the user can exclude identifiers, timestamps, free-text notes, and other fields that should not influence matching.

The user chooses one of three minimum field-level match thresholds:

- 100% match
- 90% match
- 80% match

The actual similarity between two records is calculated as:

```text
matching selected columns / total selected columns * 100
```

A pair qualifies when its calculated similarity is greater than or equal to the selected threshold. The interface shows the attainable match percentages for the chosen number of comparison columns because some thresholds cannot occur exactly when only a few columns are selected.

### 6.2 Missing-value comparison

- Two recognized missing values count as a matching field.
- A missing value compared with a populated value does not match.

### 6.3 Duplicate-group review

For every detected group, show:

- Records side by side
- Calculated match percentage
- Matching columns
- Differing columns
- Differing values highlighted

Also show the total duplicate groups, affected rows, proposed removals, and resulting row count.

### 6.4 Resolution actions

For each duplicate group, the user can:

- Keep all records
- Keep the first occurrence
- Keep the last occurrence
- Keep the most complete record
- Manually select the retained record
- Merge records

Before applying a bulk action, the tool previews which records will be retained, removed, or merged. Applied decisions are recorded in transformation history and can be undone.

When merging records:

- Identical values are retained automatically.
- When one value is missing and the other is populated, the populated value is retained.
- When two populated values conflict, the user chooses the retained value field by field.
- Conflicting populated values are never resolved automatically.

### 6.5 MVP boundary

The MVP uses field-level equality across the selected columns. It does not use fuzzy text similarity within individual fields. Values such as `Varun Jain` and `Varun J` therefore do not count as an equal field in the MVP. Fuzzy entity matching may be added as a later advanced capability.

## 7. Remaining basic cleaning

These checks complete the foundational cleaning workflow. They do not include outlier or anomaly analysis.

### 7.1 Column-name cleaning

The user can:

- Trim leading and trailing whitespace from column names
- Standardize column-name case
- Replace spaces and unsupported symbols with underscores
- Detect duplicate column names
- Review and resolve proposed column-name collisions before changes are applied

All renamed columns are shown in an original-to-new-name preview.

### 7.2 Invalid-value validation

The tool identifies values that conflict with the confirmed schema or user-defined rules, including:

- Impossible or unparseable dates
- Numeric parsing failures
- Values outside a user-defined valid range
- Values outside a user-defined allowed category list

For each validation issue, show the affected count, percentage, example rows, and available treatment. No invalid value is removed or replaced automatically.

### 7.3 Empty and constant structures

The tool detects:

- Completely empty rows
- Completely empty columns
- Constant columns containing only one distinct non-missing value

The user can retain or remove each flagged structure after reviewing the affected count and resulting dataset dimensions.

### 7.4 Basic format standardization

The user can standardize:

- Date and datetime display formats
- Decimal separators
- Boolean representations such as `Yes/No`, `Y/N`, `True/False`, and `1/0`
- Currency values by separating symbols from numeric values
- Percentage values by parsing symbols and confirming whether stored values represent proportions or percentage points

Every operation includes an affected-value count and before-and-after preview. Ambiguous interpretations require explicit user confirmation.

### 7.5 Scope boundary

Outlier detection, anomaly detection, cross-column business-rule validation, feature engineering, fuzzy entity matching, and predictive imputation are not part of the foundational cleaning layer. Outlier analysis is handled separately below.

## 8. Contextual outlier analysis

Outlier analysis uses a mandatory two-stage workflow. Univariate analysis flags statistically unusual values, but treatment actions remain unavailable until the user reviews those records in bivariate context. A statistical outlier is treated as a review signal, not proof of an error.

### 8.1 Outcome-variable declaration

Before outlier analysis, the user can declare the dataset's outcome variable, such as churn, profit, conversion, sales, or customer lifetime value. Declaring an outcome is optional because some cleaning tasks do not have a modeling or decision outcome.

When an outcome is declared, the user specifies:

- Outcome column
- Outcome meaning and data type
- Positive class for binary or categorical outcomes, such as `Churn = Yes`

The declared column receives a persistent outcome-role label and is recommended prominently during bivariate analysis.

An outcome declaration is required before outcome-focused bivariate analysis becomes available. Contextual bivariate analysis remains available when the user selects `No outcome variable`.

Bivariate review distinguishes two purposes:

- Outcome relationship: whether an unusual value has a meaningful relationship with the declared outcome
- Contextual validation: whether another variable explains why the value appears extreme

The outcome variable does not replace other contextual variables. A record is not retained, removed, or treated solely because it is associated with the outcome.

Supported outcome comparisons include:

- Numeric variable against numeric outcome: scatter plot and association measure
- Numeric variable against binary outcome: grouped distributions or box plots
- Categorical variable against numeric outcome: outcome distribution by category
- Categorical variable against binary outcome: outcome rate by category

### 8.2 Stage 1: univariate screening

For each numeric column, show:

- Histogram of the distribution
- Mean and median markers
- Lower and upper statistical boundaries
- Flagged values highlighted on the histogram
- Outlier count and percentage
- Minimum, maximum, mean, median, standard deviation, Q1, Q3, IQR, and skewness

The user can switch between:

- IQR boundaries
- Z-score
- Modified Z-score using the median and MAD
- User-selected percentile boundaries
- User-defined minimum and maximum values

Version 1 defaults are:

- Default method: IQR
- IQR multiplier: 1.5
- Z-score threshold: absolute value greater than 3.0
- Modified Z-score threshold: absolute value greater than 3.5

The histogram, boundaries, and flagged-record count update when the method or threshold changes. Statistical boundaries do not override business-validity rules established during invalid-value validation.

At the univariate stage, the user can only:

- Mark the column and flagged records for bivariate review
- Keep all flagged records
- Exclude the column from outlier analysis

Removal, replacement, capping, and imputation remain disabled.

### 8.3 Stage 2: bivariate contextual review

The user selects a second variable that may explain the flagged values. The tool recommends useful candidates while excluding unique identifiers, constant columns, free-text columns, columns with excessive missingness, and the numeric column already being investigated.

Candidate recommendations can use numeric association, temporal ordering, categorical groups with sufficient observations, and shared non-missing row coverage. Each recommendation states why it was suggested and reports the number of usable paired rows.

The visualization and supporting evidence depend on the context variable:

- Numeric against numeric: scatter plot, association measure, optional trend line, and residual distance from the trend
- Numeric against categorical: box plots or distributions by category, group median, group IQR, sample size, and the flagged record's position within its group
- Numeric against datetime: time-series plot, optional rolling median, optional rolling IQR band, date gaps, and repeated timestamps
- Numeric against boolean: two-group box plot or distribution comparison with group medians and IQRs

Records flagged during univariate screening remain highlighted in the bivariate visualization. The panel also displays the relevant values from both variables so the user can inspect each flagged record in context.

Examples include salary against years of experience, order value by customer type, property price against square footage, and daily sales over time.

Groups or paired datasets with too few usable observations display an insufficient-evidence warning rather than a confident contextual result.

### 8.4 Review classification

After bivariate review, the user classifies flagged records as:

- Valid extreme
- Likely data error
- Needs manual review
- Excluded from the selected relationship

The interface describes records as statistically unusual or inconsistent with the selected relationship. It does not claim that bivariate analysis proves a record is incorrect.

The tool can summarize the evidence for each record as:

- Explained by context: unusual globally but consistent with the selected relationship
- Still unusual: unusual globally and within the selected context
- Conflicting evidence: different contextual variables support different interpretations
- Insufficient evidence: the paired-data or group sample is too small

The user can test multiple contextual variables before making a final decision. Results from each pairwise analysis are retained alongside the record so the user can compare the evidence. The first version remains limited to interpretable pairwise analysis; simultaneous multivariate outlier detection is deferred.

### 8.5 Treatment after contextual review

Only after bivariate review can the user:

- Keep the record
- Remove its row
- Replace the value with missing
- Cap the value at the selected lower or upper boundary
- Replace it using an available imputation method
- Enter a corrected value

Before application, show the affected records and a before-and-after preview. After treatment, compare the original and resulting distributions and report changes in mean, median, standard deviation, minimum, maximum, row count, and remaining flagged values. Every decision is recorded in transformation history and can be undone.

## 9. Final validation and export

### 9.1 Final validation

Before export, the tool reruns all applicable quality checks against the current cleaned dataset rather than relying only on results calculated earlier in the workflow.

The final screen compares the original and cleaned datasets, including:

- Row count
- Column count
- Missing-value count
- Duplicate-record count
- Invalid-value count
- Flagged and unresolved outlier count
- Total cells changed
- Rows removed
- Columns removed
- Columns modified

It also lists completed steps, skipped steps, unresolved warnings, and remaining missing, invalid, duplicate, or outlier issues.

### 9.2 Unresolved-warning safeguard

Unresolved warnings do not block export. If warnings remain, the tool clearly summarizes them and requires the user to acknowledge that they are exporting a dataset with unresolved issues before enabling the final export action.

The acknowledgement is recorded in the exported transformation metadata and summary report.

### 9.3 Downloadable change summary

The user can download a human-readable HTML summary report containing:

- Dataset name and cleaning timestamp
- Original and final dimensions
- Before-and-after quality summary
- Ordered transformation history
- Column-level changes
- Rows and columns removed
- Missing-value treatments
- Data-type conversions
- Normalization operations
- Duplicate-resolution decisions
- Outlier-review methods, evidence statuses, and treatments
- Skipped steps and unresolved warnings
- Export configuration and tool version

The report summarizes affected counts and applied values without including sensitive row-level data by default. A PDF version may be added later as a secondary format.

### 9.4 Export formats

The user can download:

- Cleaned dataset as CSV
- Cleaned dataset as XLSX
- Cleaning summary as HTML
- Transformation configuration as JSON
- Reproducible Python cleaning script
- Optional separate export of flagged or unresolved records

Files can be downloaded individually or together as a ZIP package.

The reproducible script uses Polars rather than generating parallel pandas and Polars versions. The input path is parameterized, deterministic transformations are reproduced in execution order, manual row-level decisions reference stable internal row identifiers, and validation assertions confirm expected schema and row-level outcomes where appropriate.

## 10. Workflow navigation

After the initial data preview, the user chooses one of two modes.

### 10.1 Full cleaning

Full cleaning provides a guided sequential workflow through:

1. Missingness
2. Intended data types
3. Value normalization
4. Column names and format standardization
5. Invalid values and empty or constant structures
6. Duplicate detection
7. Optional outcome-variable declaration
8. Univariate outlier screening
9. Bivariate contextual or outcome-focused review
10. Final validation
11. Export

The user may skip a stage, but the workflow records it explicitly as skipped.

### 10.2 Specific cleaning

Specific cleaning presents every cleaning module defined in this architecture and lets the user select only the required operation or operations.

Even in specific-cleaning mode, the tool performs a lightweight, non-mutating schema and missing-marker assessment so selected modules do not operate on incorrectly interpreted values.

### 10.3 Stage states and dependencies

Each stage uses one of the following states:

- Not started
- In progress
- Completed
- Skipped
- Unresolved warnings
- Blocked by prerequisite
- Needs recalculation

If an earlier transformation changes data used by a later completed analysis, the dependent result is marked `Needs recalculation`. Stale results are never silently retained or presented as current.

## 11. Transformation and undo model

- The original uploaded dataset remains immutable throughout the session.
- Confirmed operations are applied to a separate working dataset.
- The tool records transformations in execution order.
- Undo reverses the most recently applied transformation one step at a time.
- Redo reapplies the most recently undone transformation.
- Undoing a transformation invalidates dependent downstream analyses, which are marked `Needs recalculation`.
- Transformation history, undo state, and the working dataset are autosaved during the active session.

## 12. Version 1 dataset and session constraints

### 12.1 Dataset limit

Version 1 supports datasets containing up to 100,000 records and files up to 100 MB. Files exceeding either fixed limit are rejected before the cleaning workflow begins with a clear explanation. The application never silently truncates an uploaded dataset.

There is no explicit product cap on the number of columns. A dataset may still be rejected when it cannot be processed safely within available memory or compute resources, with a resource-limit explanation.

Data calculations use all accepted records. Interactive charts may use a representative visual sample when rendering every point would reduce responsiveness, but boundaries, counts, statistics, and cleaning decisions continue to use the complete dataset.

- Histograms calculate aggregated bins from all applicable values.
- Scatter plots may display a reproducible sample plus every flagged record.
- Any visually sampled chart is clearly labelled as sampled.

### 12.2 Temporary session storage

Uploaded files, the immutable original dataset, the working dataset, and project state are stored temporarily for the active session.

- Each upload receives a unique project and session identifier.
- Confirmed transformations and workflow state are autosaved.
- Ordinary page navigation and browser refresh do not lose the active project.
- The same opaque session link can restore the project until it expires.
- The interface shows the session-expiry time and warns the user before expiry.
- A session expires after one hour of inactivity, with activity extending the expiry window.
- Session files and state are deleted automatically after expiry.
- The application does not rely exclusively on browser memory to preserve an active project.

For Railway deployment, temporary session directories are stored on a mounted Railway Volume so ordinary application restarts or redeployments do not erase an active session. The application runs an expiry cleanup process that deletes each session directory after its inactivity window.

## 13. Technical direction

- Development environment: Windsurf generates and supports frontend and backend implementation.
- Frontend framework: React.
- Backend framework: FastAPI.
- Primary processing engine: Polars, selected for fast dataframe operations.
- Charting library: Plotly for interactive histograms, scatter plots, box plots, distribution comparisons, and time-series charts.
- Version 1 avoids database-hosting and live database-connection requirements.
- Deployment platform: Railway.
- Temporary server-side storage: session directories on a mounted Railway Volume.

Version 1 processes operations synchronously and shows progress indicators for parsing, validation, duplicate analysis, transformations, and export. Celery and Redis are not included initially and are added only if performance testing shows that synchronous requests cannot complete reliably within the processing timeout.

## 14. Recommendation defaults

- Strongly skewed numeric distribution: absolute skewness greater than or equal to 1.0
- Minimum usable records for group-based imputation: 5 per group
- Minimum usable records for a bivariate group conclusion: 10 per group
- Maximum missingness for an automatically suggested contextual variable: 40%
- Outlier thresholds follow the defaults in the univariate-analysis section

Recommendations remain advisory and never apply a transformation automatically.

## 15. Transformation dependency map

When an earlier operation changes data used by a later stage, dependent results are invalidated and marked `Needs recalculation`.

- Missing-value treatment invalidates type inference, normalization compatibility, duplicates, statistics, univariate analysis, bivariate analysis, and final validation.
- Type conversion invalidates normalization compatibility, format validation, duplicates, statistics, univariate analysis, bivariate analysis, and final validation.
- Value normalization invalidates missingness counts, categorical validation, duplicates, and final validation.
- Column renaming invalidates saved column references throughout configuration and export; references are remapped when unambiguous and otherwise require review.
- Invalid-value treatment invalidates missingness, statistics, duplicates, univariate analysis, bivariate analysis, and final validation.
- Empty-row, empty-column, or constant-column removal invalidates dataset dimensions, relevant column references, duplicates, statistics, outlier analysis, and final validation.
- Duplicate removal or merging invalidates every dataset-level count, distribution, univariate result, bivariate result, and final validation.
- Outcome-variable changes invalidate outcome-focused bivariate results but not contextual bivariate results.
- Outlier treatment invalidates statistics, affected univariate and bivariate results, and final validation.

## 16. Security requirements

- Validate the file extension, MIME type, and file signature before processing.
- Reject corrupted or password-protected workbooks.
- Never execute workbook macros, embedded code, or uploaded scripts.
- Sanitize uploaded and exported filenames.
- Protect CSV and XLSX exports against spreadsheet formula injection.
- Generate cryptographically random, unguessable session tokens.
- Authorize every file and project request against its session token.
- Prevent one session from reading or modifying another session's files.
- Delete partial files and incomplete state after failed uploads or processing jobs.
- Rate-limit upload and processing endpoints.
- Apply the fixed file-size and row-count limits before full processing.

## 17. Testing requirements

Maintain deliberately dirty reference datasets covering every supported cleaning operation and expected warning state. Automated and manual testing must include:

- Expected result for each transformation
- Undo and redo correctness
- Dependency invalidation and recalculation
- Multi-sheet checklist, separate histories, and reconstructed workbook export
- CSV delimiter and encoding detection with manual correction
- Refresh and opaque-link session recovery
- One-hour inactivity expiry and cleanup
- 100 MB upload rejection
- 100,001-row rejection
- No silent truncation
- Full-data statistics with sampled visual rendering
- Reproducible Polars-script output
- Formula-injection protection
- Cross-session access rejection
- Final report and ZIP contents
- Performance measurement at the 100,000-row boundary

Maximum processing-time and Railway memory targets are set after benchmark testing with representative narrow, wide, numeric-heavy, text-heavy, and multi-sheet datasets.
