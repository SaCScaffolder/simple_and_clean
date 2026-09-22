package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// makeFixture creates a temp directory with the given files and returns the path.
// Each entry in files is a relative path; content "" means create empty.
func makeFixture(t *testing.T, files map[string]string) string {
	t.Helper()
	root := t.TempDir()
	for rel, body := range files {
		full := filepath.Join(root, rel)
		if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
			t.Fatalf("mkdir: %v", err)
		}
		if err := os.WriteFile(full, []byte(body), 0o644); err != nil {
			t.Fatalf("write %s: %v", rel, err)
		}
	}
	return root
}

func TestValidate_MinimalLocalService(t *testing.T) {
	root := makeFixture(t, map[string]string{
		"Dockerfile":                "FROM scratch AS build\nFROM alpine\n",
		".gitignore":                "node_modules/\n",
		".gitleaks.toml":            "[allowlist]\n",
		".env.example":              "PORT=8080\n",
		"README.md":                 "# svc\n",
		".github/workflows/ci.yml":  "name: ci\n",
	})

	rep := Validate(root)
	if rep.DeployTarget != "local" {
		t.Errorf("deploy_target = %q, want local", rep.DeployTarget)
	}
	if rep.Summary.Errors != 0 {
		t.Errorf("expected 0 errors, got %d (checks: %+v)", rep.Summary.Errors, rep.Checks)
	}
	if rep.ExitCode != ExitOK {
		t.Errorf("exit_code = %d, want %d", rep.ExitCode, ExitOK)
	}
}

func TestValidate_AzureServiceWithInfra(t *testing.T) {
	root := makeFixture(t, map[string]string{
		"Dockerfile":                 "FROM scratch AS build\nFROM alpine\n",
		".gitignore":                 "*.pyc\n",
		".gitleaks.toml":             "[allowlist]\n",
		".env.example":               "PORT=8080\n",
		"README.md":                  "# svc\n",
		"infra/main.bicep":           "param location string = 'eastus'\n",
		"infra/dev.bicepparam":       "env = 'dev'\n",
		"infra/staging.bicepparam":   "env = 'staging'\n",
		"infra/prod.bicepparam":      "env = 'prod'\n",
		".github/workflows/ci.yml":   "name: ci\n",
	})

	rep := Validate(root)
	if rep.DeployTarget != "azure" {
		t.Errorf("deploy_target = %q, want azure", rep.DeployTarget)
	}
	if rep.Summary.Errors != 0 {
		t.Errorf("expected 0 errors, got %d", rep.Summary.Errors)
	}
	// Multi-stage should be detected.
	for _, c := range rep.Checks {
		if c.Name == "dockerfile:multi-stage" && !c.Passed {
			t.Errorf("multi-stage check should pass with 2 FROM stages")
		}
	}
}

func TestValidate_MissingRequiredFiles(t *testing.T) {
	root := makeFixture(t, map[string]string{
		"Dockerfile": "FROM scratch\n",
		// intentionally missing: .gitignore, .gitleaks.toml, .env.example, README.md
	})

	rep := Validate(root)
	if rep.Summary.Errors < 4 {
		t.Errorf("expected at least 4 errors from missing files, got %d", rep.Summary.Errors)
	}
	if rep.ExitCode != ExitError {
		t.Errorf("exit_code = %d, want %d", rep.ExitCode, ExitError)
	}
}

func TestValidate_DockerfileMultiStage(t *testing.T) {
	cases := []struct {
		name    string
		docker  string
		passed  bool
		message string
	}{
		{
			name:   "two-stage",
			docker: "FROM golang:1.22 AS build\nCOPY . /src\nRUN go build\nFROM alpine\nCOPY --from=build /bin /bin\n",
			passed: true,
		},
		{
			name:   "three-stage",
			docker: "FROM node:20 AS deps\nFROM node:20 AS build\nFROM nginx:alpine\n",
			passed: true,
		},
		{
			name:   "single-stage",
			docker: "FROM node:20\nCOPY . /app\nCMD node app.js\n",
			passed: false,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			root := makeFixture(t, map[string]string{
				"Dockerfile": tc.docker,
			})
			rep := Validate(root)
			for _, c := range rep.Checks {
				if c.Name == "dockerfile:multi-stage" {
					if c.Passed != tc.passed {
						t.Errorf("multi-stage passed = %v, want %v (msg=%q)", c.Passed, tc.passed, c.Message)
					}
					return
				}
			}
			t.Fatalf("multi-stage check not found")
		})
	}
}

func TestValidate_PlaintextSecretDetection(t *testing.T) {
	root := makeFixture(t, map[string]string{
		"Dockerfile": "FROM scratch\n",
		".env":       "DATABASE_PASSWORD=hunter2\n",
	})

	rep := Validate(root)
	for _, c := range rep.Checks {
		if c.Name == "secrets:env-file" {
			if c.Passed {
				t.Errorf("expected plaintext secret check to FAIL, got pass: %+v", c)
			}
			if c.Severity != SeverityError {
				t.Errorf("expected severity=error, got %q", c.Severity)
			}
			return
		}
	}
	t.Fatalf("secrets:env-file check not found")
}

func TestReport_JSONShape(t *testing.T) {
	root := makeFixture(t, map[string]string{
		"Dockerfile":     "FROM scratch\n",
		".gitignore":     "x\n",
		".gitleaks.toml": "x\n",
		".env.example":   "x\n",
		"README.md":      "x\n",
	})

	rep := Validate(root)
	data, err := json.Marshal(rep)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	s := string(data)
	for _, want := range []string{`"path"`, `"deploy_target"`, `"exit_code"`, `"summary"`, `"checks"`} {
		if !strings.Contains(s, want) {
			t.Errorf("JSON missing field %s in %s", want, s)
		}
	}
}

func TestSummarize(t *testing.T) {
	checks := []Check{
		{Name: "a", Severity: SeverityError, Passed: true},
		{Name: "b", Severity: SeverityError, Passed: false},
		{Name: "c", Severity: SeverityWarn, Passed: false},
		{Name: "d", Severity: SeverityWarn, Passed: true},
		{Name: "e", Severity: SeverityInfo, Passed: false},
	}
	s := summarize(checks)
	if s.Passed != 2 {
		t.Errorf("passed = %d, want 2", s.Passed)
	}
	if s.Errors != 1 {
		t.Errorf("errors = %d, want 1", s.Errors)
	}
	if s.Warnings != 1 {
		t.Errorf("warnings = %d, want 1", s.Warnings)
	}
	if s.Info != 1 {
		t.Errorf("info = %d, want 1", s.Info)
	}
}

func TestComputeExitCode(t *testing.T) {
	cases := []struct {
		name string
		sum  Summary
		want int
	}{
		{"clean", Summary{Passed: 5}, ExitOK},
		{"warnings only", Summary{Passed: 4, Warnings: 1}, ExitWarn},
		{"errors", Summary{Passed: 3, Errors: 1}, ExitError},
		{"both", Summary{Passed: 2, Warnings: 1, Errors: 1}, ExitError},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			r := Report{Summary: tc.sum}
			if got := r.computeExitCode(); got != tc.want {
				t.Errorf("exit_code = %d, want %d", got, tc.want)
			}
		})
	}
}

func TestInferDeployTarget(t *testing.T) {
	cases := []struct {
		name string
		files map[string]string
		want string
	}{
		{"azure", map[string]string{"infra/main.bicep": "x", "Dockerfile": "x"}, "azure"},
		{"local", map[string]string{"Dockerfile": "x"}, "local"},
		{"unknown", map[string]string{}, "unknown"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			root := makeFixture(t, tc.files)
			if got := inferDeployTarget(root); got != tc.want {
				t.Errorf("inferDeployTarget = %q, want %q", got, tc.want)
			}
		})
	}
}
