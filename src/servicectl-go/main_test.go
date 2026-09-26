package main

import (
	"reflect"
	"testing"
)

func TestSplitName(t *testing.T) {
	cases := []struct {
		name     string
		in       []string
		wantName string
		wantArgs []string
	}{
		{
			name:     "empty",
			in:       []string{},
			wantName: "",
			wantArgs: []string{},
		},
		{
			name:     "name only",
			in:       []string{"my-svc"},
			wantName: "my-svc",
			wantArgs: []string{},
		},
		{
			name:     "name first, then flags",
			in:       []string{"my-svc", "--template=foo"},
			wantName: "my-svc",
			wantArgs: []string{"--template=foo"},
		},
		{
			name:     "flags first, then name",
			in:       []string{"--template=foo", "my-svc"},
			wantName: "my-svc",
			wantArgs: []string{"--template=foo"},
		},
		{
			name:     "flags interleaved around name",
			in:       []string{"--template=foo", "my-svc", "--no-git"},
			wantName: "my-svc",
			wantArgs: []string{"--template=foo", "--no-git"},
		},
		{
			name:     "only flags, no name",
			in:       []string{"--template=foo", "--no-git"},
			wantName: "",
			wantArgs: []string{"--template=foo", "--no-git"},
		},
		{
			name:     "double dash separator",
			in:       []string{"--", "positional-name"},
			wantName: "positional-name",
			wantArgs: []string{},
		},
		{
			name:     "double dash with flags before and name after",
			in:       []string{"--template=foo", "--", "name"},
			wantName: "name",
			wantArgs: []string{"--template=foo"},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			gotName, gotArgs := splitName(tc.in)
			if gotName != tc.wantName {
				t.Errorf("name = %q, want %q", gotName, tc.wantName)
			}
			if !reflect.DeepEqual(gotArgs, tc.wantArgs) {
				t.Errorf("args = %v, want %v", gotArgs, tc.wantArgs)
			}
		})
	}
}

func TestValidServiceName(t *testing.T) {
	cases := []struct {
		in   string
		want bool
	}{
		// Valid.
		{"my-svc", true},
		{"billing-api", true},
		{"my.cool.api", true},
		{"a", true},
		{"svc.v1", true},
		{"my_svc", true},
		{"abc123", true},
		{"a1b2c3", true},

		// Invalid.
		{"", false},
		{"BillingAPI", false},
		{"-leading-dash", false},
		{"trailing-dash-", false},
		{"bad/name", false},
		{"with space", false},
		{"dollar$sign", false},
	}
	for _, tc := range cases {
		t.Run(tc.in, func(t *testing.T) {
			if got := validServiceName(tc.in); got != tc.want {
				t.Errorf("validServiceName(%q) = %v, want %v", tc.in, got, tc.want)
			}
		})
	}
}

func TestValidTemplate(t *testing.T) {
	cases := []struct {
		in   string
		want bool
	}{
		{"node-express", true},
		{"node-react-web", true},
		{"python-flask", true},
		{"dotnet-webapi", true},
		{"go-webapi", true},
		{"bogus", false},
		{"", false},
		{"NODE-EXPRESS", false}, // case-sensitive on purpose
	}
	for _, tc := range cases {
		t.Run(tc.in, func(t *testing.T) {
			if got := validTemplate(tc.in); got != tc.want {
				t.Errorf("validTemplate(%q) = %v, want %v", tc.in, got, tc.want)
			}
		})
	}
}