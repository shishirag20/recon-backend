--
-- PostgreSQL database dump
--

\restrict iW2p02cAI7yi90cMacfFnWm9SMEThdmOcCU7g68nMHqrNIMXyNwW0ulT2ymo7ex

-- Dumped from database version 17.10
-- Dumped by pg_dump version 17.10

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: pgcrypto; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;


--
-- Name: EXTENSION pgcrypto; Type: COMMENT; Schema: -; Owner: 
--

COMMENT ON EXTENSION pgcrypto IS 'cryptographic functions';


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: bank_statements; Type: TABLE; Schema: public; Owner: recon
--

CREATE TABLE public.bank_statements (
    bank_txn_id uuid DEFAULT gen_random_uuid() NOT NULL,
    entity_id uuid NOT NULL,
    gl_account_id uuid,
    source_job_id uuid,
    document_number text,
    line_number integer,
    bank_reference text,
    transaction_date date NOT NULL,
    value_date date,
    fiscal_year integer,
    fiscal_period smallint,
    narration text,
    payer_name text,
    payer_account_no text,
    payer_ifsc text,
    currency character(3) NOT NULL,
    amount_minor bigint NOT NULL,
    amount_home_minor bigint NOT NULL,
    fx_rate numeric(18,8),
    dr_cr text NOT NULL,
    explicit_fee_minor bigint DEFAULT 0 NOT NULL,
    is_bank_charge boolean DEFAULT false NOT NULL,
    contra_reference text,
    recon_status text DEFAULT 'PENDING'::text NOT NULL,
    gl_posted boolean DEFAULT false NOT NULL,
    raw jsonb,
    valid boolean DEFAULT true NOT NULL,
    issues text[],
    row_hash text
);


ALTER TABLE public.bank_statements OWNER TO recon;

--
-- Name: credit_debit_memos; Type: TABLE; Schema: public; Owner: recon
--

CREATE TABLE public.credit_debit_memos (
    memo_id uuid DEFAULT gen_random_uuid() NOT NULL,
    customer_id uuid NOT NULL,
    invoice_id uuid,
    memo_type text NOT NULL,
    memo_date date NOT NULL,
    currency character(3) NOT NULL,
    amount_minor bigint NOT NULL,
    amount_home_minor bigint NOT NULL,
    is_open boolean DEFAULT true NOT NULL,
    raw jsonb
);


ALTER TABLE public.credit_debit_memos OWNER TO recon;

--
-- Name: currencies; Type: TABLE; Schema: public; Owner: recon
--

CREATE TABLE public.currencies (
    code character(3) NOT NULL,
    name text NOT NULL,
    minor_unit smallint DEFAULT 2 NOT NULL
);


ALTER TABLE public.currencies OWNER TO recon;

--
-- Name: customer_bank_accounts; Type: TABLE; Schema: public; Owner: recon
--

CREATE TABLE public.customer_bank_accounts (
    account_id uuid DEFAULT gen_random_uuid() NOT NULL,
    customer_id uuid NOT NULL,
    bank_account_no text NOT NULL,
    ifsc_code text,
    alias text,
    is_primary boolean DEFAULT false NOT NULL,
    status text DEFAULT 'ACTIVE'::text NOT NULL
);


ALTER TABLE public.customer_bank_accounts OWNER TO recon;

--
-- Name: customer_reference_codes; Type: TABLE; Schema: public; Owner: recon
--

CREATE TABLE public.customer_reference_codes (
    reference_id uuid DEFAULT gen_random_uuid() NOT NULL,
    customer_id uuid NOT NULL,
    code_type text NOT NULL,
    code_value text NOT NULL,
    match_priority smallint DEFAULT 5 NOT NULL,
    is_active boolean DEFAULT true NOT NULL
);


ALTER TABLE public.customer_reference_codes OWNER TO recon;

--
-- Name: customers; Type: TABLE; Schema: public; Owner: recon
--

CREATE TABLE public.customers (
    customer_id uuid DEFAULT gen_random_uuid() NOT NULL,
    entity_id uuid NOT NULL,
    customer_code text NOT NULL,
    company_name text NOT NULL,
    pan text,
    gstin text,
    vpa_handle text,
    payment_terms text,
    credit_limit_minor bigint,
    city text,
    state text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    raw jsonb,
    source_job_id uuid,
    valid boolean DEFAULT true NOT NULL,
    issues text[]
);


ALTER TABLE public.customers OWNER TO recon;

--
-- Name: data_sources; Type: TABLE; Schema: public; Owner: recon
--

CREATE TABLE public.data_sources (
    source_id uuid DEFAULT gen_random_uuid() NOT NULL,
    entity_id uuid NOT NULL,
    name text NOT NULL,
    kind text NOT NULL,
    status text DEFAULT 'CONNECTED'::text NOT NULL,
    stream text NOT NULL
);


ALTER TABLE public.data_sources OWNER TO recon;

--
-- Name: documents; Type: TABLE; Schema: public; Owner: recon
--

CREATE TABLE public.documents (
    document_id uuid DEFAULT gen_random_uuid() NOT NULL,
    file_name text NOT NULL,
    byte_size bigint,
    mime_type text,
    category text,
    storage_uri text,
    linked_type text,
    linked_id uuid,
    uploaded_by uuid,
    uploaded_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.documents OWNER TO recon;

--
-- Name: entities; Type: TABLE; Schema: public; Owner: recon
--

CREATE TABLE public.entities (
    entity_id uuid DEFAULT gen_random_uuid() NOT NULL,
    organization_id uuid NOT NULL,
    company_code text NOT NULL,
    name text NOT NULL,
    site_code text,
    home_currency character(3) DEFAULT 'INR'::bpchar NOT NULL,
    accounting_standard text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.entities OWNER TO recon;

--
-- Name: expected_remittances; Type: TABLE; Schema: public; Owner: recon
--

CREATE TABLE public.expected_remittances (
    remittance_id uuid DEFAULT gen_random_uuid() NOT NULL,
    customer_id uuid NOT NULL,
    utr_number text,
    declared_amount_minor bigint NOT NULL,
    currency character(3) NOT NULL,
    declared_date date,
    reconciled boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    raw jsonb
);


ALTER TABLE public.expected_remittances OWNER TO recon;

--
-- Name: field_mappings; Type: TABLE; Schema: public; Owner: recon
--

CREATE TABLE public.field_mappings (
    mapping_id uuid DEFAULT gen_random_uuid() NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    source_field text NOT NULL,
    canonical_field text NOT NULL,
    transform text DEFAULT 'NONE'::text NOT NULL,
    transform_param text,
    is_active boolean DEFAULT true NOT NULL,
    stream text NOT NULL
);


ALTER TABLE public.field_mappings OWNER TO recon;

--
-- Name: fx_rates; Type: TABLE; Schema: public; Owner: recon
--

CREATE TABLE public.fx_rates (
    fx_rate_id uuid DEFAULT gen_random_uuid() NOT NULL,
    from_ccy character(3) NOT NULL,
    to_ccy character(3) NOT NULL,
    rate_date date NOT NULL,
    rate numeric(18,8) NOT NULL,
    rate_type text
);


ALTER TABLE public.fx_rates OWNER TO recon;

--
-- Name: gateway_settlements; Type: TABLE; Schema: public; Owner: recon
--

CREATE TABLE public.gateway_settlements (
    settlement_id uuid DEFAULT gen_random_uuid() NOT NULL,
    entity_id uuid NOT NULL,
    source_job_id uuid,
    gateway text NOT NULL,
    gateway_transaction_id text NOT NULL,
    customer_id uuid,
    bank_txn_id uuid,
    currency character(3) NOT NULL,
    gross_amount_minor bigint NOT NULL,
    fee_minor bigint DEFAULT 0 NOT NULL,
    gst_on_fee_minor bigint DEFAULT 0 NOT NULL,
    net_settled_minor bigint NOT NULL,
    settlement_date date NOT NULL,
    matched boolean DEFAULT false NOT NULL,
    raw jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.gateway_settlements OWNER TO recon;

--
-- Name: gl_accounts; Type: TABLE; Schema: public; Owner: recon
--

CREATE TABLE public.gl_accounts (
    gl_account_id uuid DEFAULT gen_random_uuid() NOT NULL,
    entity_id uuid NOT NULL,
    account_code text NOT NULL,
    account_name text NOT NULL,
    account_type text,
    normal_balance text,
    l1_group text,
    l2_group text,
    l3_group text,
    is_control boolean DEFAULT false NOT NULL
);


ALTER TABLE public.gl_accounts OWNER TO recon;

--
-- Name: gl_control_balances; Type: TABLE; Schema: public; Owner: recon
--

CREATE TABLE public.gl_control_balances (
    balance_id uuid DEFAULT gen_random_uuid() NOT NULL,
    gl_account_id uuid NOT NULL,
    period_date date NOT NULL,
    control_balance_minor bigint NOT NULL
);


ALTER TABLE public.gl_control_balances OWNER TO recon;

--
-- Name: gl_journal_entries; Type: TABLE; Schema: public; Owner: recon
--

CREATE TABLE public.gl_journal_entries (
    journal_id uuid DEFAULT gen_random_uuid() NOT NULL,
    entity_id uuid NOT NULL,
    run_id uuid,
    posting_date date NOT NULL,
    source_type text NOT NULL,
    memo text,
    posted_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.gl_journal_entries OWNER TO recon;

--
-- Name: gl_journal_lines; Type: TABLE; Schema: public; Owner: recon
--

CREATE TABLE public.gl_journal_lines (
    line_id uuid DEFAULT gen_random_uuid() NOT NULL,
    journal_id uuid NOT NULL,
    line_number integer NOT NULL,
    gl_account_id uuid NOT NULL,
    dr_cr text NOT NULL,
    currency character(3) NOT NULL,
    amount_minor bigint NOT NULL,
    amount_home_minor bigint NOT NULL,
    business_partner_id uuid
);


ALTER TABLE public.gl_journal_lines OWNER TO recon;

--
-- Name: immutable_audit_trail; Type: TABLE; Schema: public; Owner: recon
--

CREATE TABLE public.immutable_audit_trail (
    audit_id bigint NOT NULL,
    at timestamp with time zone DEFAULT now() NOT NULL,
    run_id uuid,
    entry_type text NOT NULL,
    category text NOT NULL,
    action text NOT NULL,
    user_id uuid,
    target_ref text,
    impact_minor bigint,
    entity_ref text,
    old_state jsonb,
    new_state jsonb,
    prev_hash text,
    row_hash text
);


ALTER TABLE public.immutable_audit_trail OWNER TO recon;

--
-- Name: immutable_audit_trail_audit_id_seq; Type: SEQUENCE; Schema: public; Owner: recon
--

CREATE SEQUENCE public.immutable_audit_trail_audit_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.immutable_audit_trail_audit_id_seq OWNER TO recon;

--
-- Name: immutable_audit_trail_audit_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: recon
--

ALTER SEQUENCE public.immutable_audit_trail_audit_id_seq OWNED BY public.immutable_audit_trail.audit_id;


--
-- Name: ingestion_jobs; Type: TABLE; Schema: public; Owner: recon
--

CREATE TABLE public.ingestion_jobs (
    job_id uuid DEFAULT gen_random_uuid() NOT NULL,
    source_id uuid,
    file_name text,
    format text,
    trigger_type text DEFAULT 'MANUAL'::text NOT NULL,
    row_count integer DEFAULT 0 NOT NULL,
    error_count integer DEFAULT 0 NOT NULL,
    status text DEFAULT 'PENDING'::text NOT NULL,
    started_by uuid,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    locked_by text,
    locked_at timestamp with time zone,
    lease_expires_at timestamp with time zone,
    attempt_count integer DEFAULT 0 NOT NULL,
    max_attempts integer DEFAULT 5 NOT NULL,
    next_attempt_at timestamp with time zone,
    last_error text,
    file_uri text,
    job_type text DEFAULT 'INGEST'::text NOT NULL,
    parent_job_id uuid,
    stream text,
    mapping_version integer,
    failed_rows jsonb,
    content_hash text,
    unmapped_columns text[]
);


ALTER TABLE public.ingestion_jobs OWNER TO recon;

--
-- Name: invitations; Type: TABLE; Schema: public; Owner: recon
--

CREATE TABLE public.invitations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organization_id uuid NOT NULL,
    email character varying(255) NOT NULL,
    role_id uuid NOT NULL,
    token_hash character varying(255) NOT NULL,
    invited_by_user_id uuid,
    expires_at timestamp with time zone NOT NULL,
    accepted_at timestamp with time zone,
    revoked_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.invitations OWNER TO recon;

--
-- Name: invoice_allocations; Type: TABLE; Schema: public; Owner: recon
--

CREATE TABLE public.invoice_allocations (
    allocation_id uuid DEFAULT gen_random_uuid() NOT NULL,
    match_group_id uuid NOT NULL,
    invoice_id uuid NOT NULL,
    payment_id uuid NOT NULL,
    bank_txn_id uuid,
    allocated_minor bigint NOT NULL,
    gl_journal_id uuid,
    allocated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT invoice_allocations_allocated_minor_check CHECK ((allocated_minor > 0))
);


ALTER TABLE public.invoice_allocations OWNER TO recon;

--
-- Name: invoices; Type: TABLE; Schema: public; Owner: recon
--

CREATE TABLE public.invoices (
    invoice_id uuid DEFAULT gen_random_uuid() NOT NULL,
    entity_id uuid NOT NULL,
    customer_id uuid NOT NULL,
    invoice_number text NOT NULL,
    issue_date date NOT NULL,
    due_date date NOT NULL,
    currency character(3) NOT NULL,
    total_amount_minor bigint NOT NULL,
    total_home_minor bigint NOT NULL,
    balance_due_minor bigint NOT NULL,
    tds_rate_pct numeric(5,2),
    allowed_tds_minor bigint DEFAULT 0 NOT NULL,
    status text DEFAULT 'OPEN'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    raw jsonb,
    source_job_id uuid,
    valid boolean DEFAULT true NOT NULL,
    issues text[]
);


ALTER TABLE public.invoices OWNER TO recon;

--
-- Name: match_groups; Type: TABLE; Schema: public; Owner: recon
--

CREATE TABLE public.match_groups (
    match_group_id uuid DEFAULT gen_random_uuid() NOT NULL,
    run_id uuid NOT NULL,
    match_type text NOT NULL,
    rule_id uuid,
    confidence smallint,
    status text DEFAULT 'AUTO_MATCHED'::text NOT NULL,
    reason text,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.match_groups OWNER TO recon;

--
-- Name: memberships; Type: TABLE; Schema: public; Owner: recon
--

CREATE TABLE public.memberships (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    organization_id uuid NOT NULL,
    role_id uuid NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.memberships OWNER TO recon;

--
-- Name: organizations; Type: TABLE; Schema: public; Owner: recon
--

CREATE TABLE public.organizations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name character varying(120) NOT NULL,
    slug character varying(60) NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_by_user_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.organizations OWNER TO recon;

--
-- Name: payments; Type: TABLE; Schema: public; Owner: recon
--

CREATE TABLE public.payments (
    payment_id uuid DEFAULT gen_random_uuid() NOT NULL,
    bank_txn_id uuid NOT NULL,
    customer_id uuid,
    total_received_minor bigint NOT NULL,
    unapplied_minor bigint NOT NULL,
    locked_by_rule_id uuid,
    candidate_pool jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.payments OWNER TO recon;

--
-- Name: permissions; Type: TABLE; Schema: public; Owner: recon
--

CREATE TABLE public.permissions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    code character varying(100) NOT NULL,
    description character varying(255)
);


ALTER TABLE public.permissions OWNER TO recon;

--
-- Name: reconciliation_definitions; Type: TABLE; Schema: public; Owner: recon
--

CREATE TABLE public.reconciliation_definitions (
    definition_id uuid DEFAULT gen_random_uuid() NOT NULL,
    entity_id uuid NOT NULL,
    name text NOT NULL,
    recon_type text NOT NULL,
    cadence text,
    owner_user_id uuid
);


ALTER TABLE public.reconciliation_definitions OWNER TO recon;

--
-- Name: reconciliation_exceptions; Type: TABLE; Schema: public; Owner: recon
--

CREATE TABLE public.reconciliation_exceptions (
    exception_id uuid DEFAULT gen_random_uuid() NOT NULL,
    run_id uuid NOT NULL,
    exception_no text,
    exception_type text NOT NULL,
    bank_txn_id uuid,
    invoice_id uuid,
    customer_id uuid,
    discrepancy_minor bigint,
    reason_code text,
    status text DEFAULT 'OPEN'::text NOT NULL,
    resolution_outcome text,
    resolver_id uuid,
    resolution_notes text,
    resolved_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.reconciliation_exceptions OWNER TO recon;

--
-- Name: reconciliation_rules; Type: TABLE; Schema: public; Owner: recon
--

CREATE TABLE public.reconciliation_rules (
    rule_id uuid DEFAULT gen_random_uuid() NOT NULL,
    definition_id uuid NOT NULL,
    phase text NOT NULL,
    kind text NOT NULL,
    name text NOT NULL,
    priority integer NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    confidence smallint,
    config jsonb DEFAULT '{}'::jsonb NOT NULL
);


ALTER TABLE public.reconciliation_rules OWNER TO recon;

--
-- Name: reconciliation_runs; Type: TABLE; Schema: public; Owner: recon
--

CREATE TABLE public.reconciliation_runs (
    run_id uuid DEFAULT gen_random_uuid() NOT NULL,
    definition_id uuid NOT NULL,
    run_no text NOT NULL,
    period_start date,
    period_end date,
    status text DEFAULT 'DRAFT'::text NOT NULL,
    volume integer,
    matched_count integer,
    exception_count integer,
    matched_value_minor bigint,
    exception_value_minor bigint,
    unapplied_minor bigint,
    prepared_by uuid,
    reviewed_by uuid,
    signed_at timestamp with time zone,
    run_hash text,
    started_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.reconciliation_runs OWNER TO recon;

--
-- Name: role_permissions; Type: TABLE; Schema: public; Owner: recon
--

CREATE TABLE public.role_permissions (
    role_id uuid NOT NULL,
    permission_id uuid NOT NULL
);


ALTER TABLE public.role_permissions OWNER TO recon;

--
-- Name: roles; Type: TABLE; Schema: public; Owner: recon
--

CREATE TABLE public.roles (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name character varying(50) NOT NULL,
    description character varying(255),
    is_system boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.roles OWNER TO recon;

--
-- Name: schema_migrations; Type: TABLE; Schema: public; Owner: recon
--

CREATE TABLE public.schema_migrations (
    filename text NOT NULL,
    applied_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.schema_migrations OWNER TO recon;

--
-- Name: sessions; Type: TABLE; Schema: public; Owner: recon
--

CREATE TABLE public.sessions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    organization_id uuid NOT NULL,
    refresh_token_hash character varying(255) NOT NULL,
    user_agent character varying(255),
    ip_address character varying(45),
    issued_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    last_used_at timestamp with time zone,
    revoked_at timestamp with time zone
);


ALTER TABLE public.sessions OWNER TO recon;

--
-- Name: users; Type: TABLE; Schema: public; Owner: recon
--

CREATE TABLE public.users (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    email character varying(255) NOT NULL,
    hashed_password character varying(255) NOT NULL,
    full_name character varying(120) NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    is_platform_admin boolean DEFAULT false NOT NULL,
    last_login_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.users OWNER TO recon;

--
-- Name: v_report_matched; Type: VIEW; Schema: public; Owner: recon
--

CREATE VIEW public.v_report_matched AS
 SELECT r.run_id,
    r.run_no,
    r.status AS run_status,
    mg.match_group_id,
    mg.match_type,
    mg.status AS match_status,
    ia.allocation_id,
    ia.allocated_minor,
    i.invoice_id,
    i.invoice_number,
    c.customer_id,
    c.company_name,
    bs.bank_txn_id,
    bs.bank_reference,
    u.full_name AS matched_by
   FROM ((((((public.invoice_allocations ia
     JOIN public.match_groups mg ON ((mg.match_group_id = ia.match_group_id)))
     JOIN public.reconciliation_runs r ON ((r.run_id = mg.run_id)))
     JOIN public.invoices i ON ((i.invoice_id = ia.invoice_id)))
     JOIN public.customers c ON ((c.customer_id = i.customer_id)))
     LEFT JOIN public.bank_statements bs ON ((bs.bank_txn_id = ia.bank_txn_id)))
     LEFT JOIN public.users u ON ((u.id = mg.created_by)))
  WHERE (r.status = ANY (ARRAY['APPROVED'::text, 'CLOSED'::text]));


ALTER VIEW public.v_report_matched OWNER TO recon;

--
-- Name: v_report_runs; Type: VIEW; Schema: public; Owner: recon
--

CREATE VIEW public.v_report_runs AS
 SELECT run_id,
    run_no,
    status,
    period_start,
    period_end,
    matched_count,
    exception_count,
    matched_value_minor,
    exception_value_minor,
    unapplied_minor,
    ( SELECT users.full_name
           FROM public.users
          WHERE (users.id = r.prepared_by)) AS prepared_by_name,
    ( SELECT users.full_name
           FROM public.users
          WHERE (users.id = r.reviewed_by)) AS reviewed_by_name,
    signed_at,
    run_hash
   FROM public.reconciliation_runs r;


ALTER VIEW public.v_report_runs OWNER TO recon;

--
-- Name: immutable_audit_trail audit_id; Type: DEFAULT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.immutable_audit_trail ALTER COLUMN audit_id SET DEFAULT nextval('public.immutable_audit_trail_audit_id_seq'::regclass);


--
-- Data for Name: bank_statements; Type: TABLE DATA; Schema: public; Owner: recon
--

COPY public.bank_statements (bank_txn_id, entity_id, gl_account_id, source_job_id, document_number, line_number, bank_reference, transaction_date, value_date, fiscal_year, fiscal_period, narration, payer_name, payer_account_no, payer_ifsc, currency, amount_minor, amount_home_minor, fx_rate, dr_cr, explicit_fee_minor, is_bank_charge, contra_reference, recon_status, gl_posted, raw, valid, issues, row_hash) FROM stdin;
e978ea9f-9f7b-4eca-a028-1b276fee85d4	5d2a38a6-e92b-4dea-887f-f786ed4c5143	\N	14e8b510-3a90-40be-a374-7cedb925123e	\N	\N	UTR-ADV-2299	2026-06-20	\N	\N	\N	\N	Some Pvt. Ltd.	\N	\N	INR	10000000	10000000	\N	CREDIT	0	f	\N	PENDING	f	{"amount": "100000", "payer_name": "Some Pvt. Ltd.", "bank_txn_id": "BANK-901", "transaction_date": "2026-06-20", "bank_reference_number": "UTR-ADV-2299"}	t	\N	c9034b4e681725d0e0e62518b01a974a29d190ecd4059ff3085162d6356df00f
a9f60a80-40d1-4fbb-8daf-b0f8bfd74f25	5d2a38a6-e92b-4dea-887f-f786ed4c5143	\N	14e8b510-3a90-40be-a374-7cedb925123e	\N	\N	NEFT-445566	2026-06-29	\N	\N	\N	\N	Apex Logistics	\N	\N	INR	14000000	14000000	\N	CREDIT	0	f	\N	PENDING	f	{"amount": "140000", "payer_name": "Apex Logistics", "bank_txn_id": "BANK-902", "transaction_date": "2026-06-29", "bank_reference_number": "NEFT-445566"}	t	\N	698525ff2d82d63c4a8d26220e698a67149ecc269ceb55ca1fb9b84a6d947fda
4e211ab2-ec81-4dbb-b67e-355eb6e9a4ad	5d2a38a6-e92b-4dea-887f-f786ed4c5143	\N	14e8b510-3a90-40be-a374-7cedb925123e	\N	\N	LUMP-556677	2026-06-30	\N	\N	\N	\N	Acme Corp Pvt. Ltd.	\N	\N	INR	20000000	20000000	\N	CREDIT	0	f	\N	PENDING	f	{"amount": "200000", "payer_name": "Acme Corp Pvt. Ltd.", "bank_txn_id": "BANK-906", "transaction_date": "2026-06-30", "bank_reference_number": "LUMP-556677"}	t	\N	a742b66d1c9535f93d7485da6d8d715f4c6f491263438a69dc8022628163f5b8
94247b1c-78c6-4a70-8fd7-0e3345bf4e19	5d2a38a6-e92b-4dea-887f-f786ed4c5143	\N	89e74967-9b16-4f31-9a1b-f421cee5aafa	\N	\N	UTR-ADV-7001	2026-07-02	\N	\N	\N	\N	Acme Industries Pvt Ltd	\N	\N	INR	1000000	1000000	\N	CREDIT	0	f	\N	PENDING	f	{"amount": "10000", "narration": "NEFT PAYMENT ACME", "payer_ifsc": "", "payer_name": "Acme Industries Pvt Ltd", "bank_txn_id": "BANK-001", "explicit_fee": "0", "is_bank_charge": "false", "clearing_status": "Pending", "transaction_date": "2026-07-02", "payer_account_number": "", "bank_reference_number": "UTR-ADV-7001"}	t	\N	0ba290cf7a41c2b472ee4dad0b7b3a8fe2a3d6a82f9bf8edbb745f30139b53e9
679a0c17-8ab6-4d4f-8422-addb865f3b45	5d2a38a6-e92b-4dea-887f-f786ed4c5143	\N	89e74967-9b16-4f31-9a1b-f421cee5aafa	\N	\N	NEFT-BT-002	2026-07-03	\N	\N	\N	\N	Bright Textiles Pvt Ltd	\N	\N	INR	1350000	1350000	\N	CREDIT	0	f	\N	PENDING	f	{"amount": "13500", "narration": "NEFT SETTLEMENT", "payer_ifsc": "HDFC0001234", "payer_name": "Bright Textiles Pvt Ltd", "bank_txn_id": "BANK-002", "explicit_fee": "0", "is_bank_charge": "false", "clearing_status": "Pending", "transaction_date": "2026-07-03", "payer_account_number": "112233445566", "bank_reference_number": "NEFT-BT-002"}	t	\N	fe60cc6f5e3132dd443e5aec352d0c6a4699a551e6e14be5ca98d6b681c43b7f
48681fbd-2db5-413a-bd64-7e64089cc34d	5d2a38a6-e92b-4dea-887f-f786ed4c5143	\N	89e74967-9b16-4f31-9a1b-f421cee5aafa	\N	\N	NEFT-BT-003	2026-07-06	\N	\N	\N	\N	Bright Textiles Pvt Ltd	\N	\N	INR	950000	950000	\N	CREDIT	0	f	\N	PENDING	f	{"amount": "9500", "narration": "OVERPAYMENT SETTLEMENT", "payer_ifsc": "HDFC0001234", "payer_name": "Bright Textiles Pvt Ltd", "bank_txn_id": "BANK-003", "explicit_fee": "0", "is_bank_charge": "false", "clearing_status": "Pending", "transaction_date": "2026-07-06", "payer_account_number": "112233445566", "bank_reference_number": "NEFT-BT-003"}	t	\N	861943e1358286c1bee8c60e276b6d869912ec8e108a0dbbb90dc01334720027
ee69ec71-e8d0-4f4f-a9cd-1bd49b912d60	5d2a38a6-e92b-4dea-887f-f786ed4c5143	\N	89e74967-9b16-4f31-9a1b-f421cee5aafa	\N	\N	NEFT-BT-004	2026-07-07	\N	\N	\N	\N	Bright Textiles Pvt Ltd	\N	\N	INR	598000	598000	\N	CREDIT	0	f	\N	PENDING	f	{"amount": "5980", "narration": "FEE ADJUSTED PAYMENT", "payer_ifsc": "HDFC0001234", "payer_name": "Bright Textiles Pvt Ltd", "bank_txn_id": "BANK-004", "explicit_fee": "20", "is_bank_charge": "false", "clearing_status": "Pending", "transaction_date": "2026-07-07", "payer_account_number": "112233445566", "bank_reference_number": "NEFT-BT-004"}	t	\N	4a4c622517e12d040bdfb659c717bc871e4d4f43c09e84d250346786ba4c06bf
512d00a0-102f-4eb3-8ad1-8ad42b3e35fa	5d2a38a6-e92b-4dea-887f-f786ed4c5143	\N	89e74967-9b16-4f31-9a1b-f421cee5aafa	\N	\N	NEFT-BT-014	2026-07-08	\N	\N	\N	\N	Bright Textiles Pvt Ltd	\N	\N	INR	299800	299800	\N	CREDIT	0	f	\N	PENDING	f	{"amount": "2998", "narration": "WRITE OFF TEST PAYMENT", "payer_ifsc": "HDFC0001234", "payer_name": "Bright Textiles Pvt Ltd", "bank_txn_id": "BANK-014", "explicit_fee": "0", "is_bank_charge": "false", "clearing_status": "Pending", "transaction_date": "2026-07-08", "payer_account_number": "112233445566", "bank_reference_number": "NEFT-BT-014"}	t	\N	fcb7a2d7ee3d7bbd398b73691ad464fd31e9e2b6c5f5d8af745cd7d621775103
d68b1ca0-8e98-4f0b-a061-ddc578f4872c	5d2a38a6-e92b-4dea-887f-f786ed4c5143	\N	89e74967-9b16-4f31-9a1b-f421cee5aafa	\N	\N	UTR-NIM-005	2026-07-04	\N	\N	\N	\N	Nimbus Traders	\N	\N	INR	800000	800000	\N	CREDIT	0	f	\N	PENDING	f	{"amount": "8000", "narration": "UPI/nimbus@okhdfc/PAYMENT INV-2026-105", "payer_ifsc": "", "payer_name": "Nimbus Traders", "bank_txn_id": "BANK-005", "explicit_fee": "0", "is_bank_charge": "false", "clearing_status": "Pending", "transaction_date": "2026-07-04", "payer_account_number": "", "bank_reference_number": "UTR-NIM-005"}	t	\N	0c85eff21e8a3cb21d5c561211c04674367ad0a1ffffd8656b1ee08fc4dd2bad
1dfe3f95-4464-4b67-8b33-a5050d79e659	5d2a38a6-e92b-4dea-887f-f786ed4c5143	\N	89e74967-9b16-4f31-9a1b-f421cee5aafa	\N	\N	UTR-NIM-006	2026-07-17	\N	\N	\N	\N	Nimbus Traders	\N	\N	INR	250000	250000	\N	CREDIT	0	f	\N	PENDING	f	{"amount": "2500", "narration": "UPI/nimbus@okhdfc/PARTIAL SETTLEMENT", "payer_ifsc": "", "payer_name": "Nimbus Traders", "bank_txn_id": "BANK-006", "explicit_fee": "0", "is_bank_charge": "false", "clearing_status": "Pending", "transaction_date": "2026-07-17", "payer_account_number": "", "bank_reference_number": "UTR-NIM-006"}	t	\N	7f665778cef3ec7ddfeba758a761fe4b50f16a05304dc82932771854569baa3f
dd29ef4d-8a02-42ee-8aa2-da4cc758b210	5d2a38a6-e92b-4dea-887f-f786ed4c5143	\N	89e74967-9b16-4f31-9a1b-f421cee5aafa	\N	\N	NEFT-KF-007	2026-07-05	\N	\N	\N	\N	Kestrel Freight Co	\N	\N	INR	1200000	1200000	\N	CREDIT	0	f	\N	PENDING	f	{"amount": "12000", "narration": "NEFT TRANSFER REF KEST04 INVC 1046", "payer_ifsc": "", "payer_name": "Kestrel Freight Co", "bank_txn_id": "BANK-007", "explicit_fee": "0", "is_bank_charge": "false", "clearing_status": "Pending", "transaction_date": "2026-07-05", "payer_account_number": "", "bank_reference_number": "NEFT-KF-007"}	t	\N	76cbee9a91b2c8d39c09d1dc00fa2efcae04a31dbfcbd5ac574a8d69b2c05123
2d9b1f96-fced-41ad-84e7-25deb771f379	5d2a38a6-e92b-4dea-887f-f786ed4c5143	\N	89e74967-9b16-4f31-9a1b-f421cee5aafa	\N	\N	NEFT-KF-008	2026-06-21	\N	\N	\N	\N	Kestrel Freight Co	\N	\N	INR	500000	500000	\N	CREDIT	0	f	\N	PENDING	f	{"amount": "5000", "narration": "NEFT TRANSFER REF KEST04 SHORT PAY INV-2026-107", "payer_ifsc": "", "payer_name": "Kestrel Freight Co", "bank_txn_id": "BANK-008", "explicit_fee": "0", "is_bank_charge": "false", "clearing_status": "Pending", "transaction_date": "2026-06-21", "payer_account_number": "", "bank_reference_number": "NEFT-KF-008"}	t	\N	c743d6719727c8273731eec9c428a7e3f2f30358bb92079ca201d3d8fb12ea80
3741d7b7-17d4-4000-be43-a7d42207bd60	5d2a38a6-e92b-4dea-887f-f786ed4c5143	\N	89e74967-9b16-4f31-9a1b-f421cee5aafa	\N	\N	UTR-SOL-009	2026-07-09	\N	\N	\N	\N	Solace Pharma Ltd	\N	\N	INR	1200000	1200000	\N	CREDIT	0	f	\N	PENDING	f	{"amount": "12000", "narration": "RTGS FROM SOLACE GSTIN 27AASCS1234F1Z5", "payer_ifsc": "", "payer_name": "Solace Pharma Ltd", "bank_txn_id": "BANK-009", "explicit_fee": "0", "is_bank_charge": "false", "clearing_status": "Pending", "transaction_date": "2026-07-09", "payer_account_number": "", "bank_reference_number": "UTR-SOL-009"}	t	\N	f9b6770b533b982de386b7abe26a5cd7307ee372cb1991f1ffe335f40cfdac82
0caf0da9-c85d-4b09-91da-f1bbd18f57a3	5d2a38a6-e92b-4dea-887f-f786ed4c5143	\N	89e74967-9b16-4f31-9a1b-f421cee5aafa	\N	\N	UTR-VAN-010	2026-07-10	\N	\N	\N	\N	Vantage Retail Solution	\N	\N	INR	900000	900000	\N	CREDIT	0	f	\N	PENDING	f	{"amount": "9000", "narration": "NEFT PAYMENT VANTAGE", "payer_ifsc": "", "payer_name": "Vantage Retail Solution", "bank_txn_id": "BANK-010", "explicit_fee": "0", "is_bank_charge": "false", "clearing_status": "Pending", "transaction_date": "2026-07-10", "payer_account_number": "", "bank_reference_number": "UTR-VAN-010"}	t	\N	62a8fd2b74b351ddc0245b1afc60da205a8dd669393f507db8200920f8b582b7
63e314bc-7fc2-4d82-ac16-318115898226	5d2a38a6-e92b-4dea-887f-f786ed4c5143	\N	89e74967-9b16-4f31-9a1b-f421cee5aafa	\N	\N	UTR-HAL-011	2026-07-11	\N	\N	\N	\N	XYZ Remitter Co	\N	\N	INR	600000	600000	\N	CREDIT	0	f	\N	PENDING	f	{"amount": "6000", "narration": "NEFT TRANSFER HALCYON SETTLEMENT", "payer_ifsc": "", "payer_name": "XYZ Remitter Co", "bank_txn_id": "BANK-011", "explicit_fee": "0", "is_bank_charge": "false", "clearing_status": "Pending", "transaction_date": "2026-07-11", "payer_account_number": "", "bank_reference_number": "UTR-HAL-011"}	t	\N	be0d0f92778e65de7737f23f968d67f6c01de5ce5ceedb6658bd1bec71cda62f
db907def-9da0-4cc3-a006-a29ea2ae000f	5d2a38a6-e92b-4dea-887f-f786ed4c5143	\N	89e74967-9b16-4f31-9a1b-f421cee5aafa	\N	\N	UTR-MER-012	2026-07-13	\N	\N	\N	\N	Random Remitter Ltd	\N	\N	INR	1100000	1100000	\N	CREDIT	0	f	\N	PENDING	f	{"amount": "11000", "narration": "GENERIC TRANSFER", "payer_ifsc": "", "payer_name": "Random Remitter Ltd", "bank_txn_id": "BANK-012", "explicit_fee": "0", "is_bank_charge": "false", "clearing_status": "Pending", "transaction_date": "2026-07-13", "payer_account_number": "334455665544", "bank_reference_number": "UTR-MER-012"}	t	\N	871b01167ab7e329b79adc33da6153624f65a8a17dc553d715ed0dcd465106d5
e1e2e116-d012-446b-a69c-b8d172ed545f	5d2a38a6-e92b-4dea-887f-f786ed4c5143	\N	89e74967-9b16-4f31-9a1b-f421cee5aafa	\N	\N	UTR-SIL-013	2026-07-14	\N	\N	\N	\N	Silverline Remit Co	\N	\N	INR	1800000	1800000	\N	CREDIT	0	f	\N	PENDING	f	{"amount": "18000", "narration": "GENERIC SETTLEMENT PAYMENT", "payer_ifsc": "", "payer_name": "Silverline Remit Co", "bank_txn_id": "BANK-013", "explicit_fee": "0", "is_bank_charge": "false", "clearing_status": "Pending", "transaction_date": "2026-07-14", "payer_account_number": "112233447788", "bank_reference_number": "UTR-SIL-013"}	t	\N	fe447f36e56e758cc667d6a903ae204f84c6459932e2c94a03be2f0b6e95a832
54add5a2-99c9-455d-8942-a572bb93e82b	5d2a38a6-e92b-4dea-887f-f786ed4c5143	\N	89e74967-9b16-4f31-9a1b-f421cee5aafa	\N	\N	UTR-COR-020	2026-07-15	\N	\N	\N	\N	Coral Living Pvt Ltd	\N	\N	INR	450000	450000	\N	CREDIT	0	f	\N	PENDING	f	{"amount": "4500", "narration": "NEFT TRANSFER REF CORL12 PAYMENT", "payer_ifsc": "", "payer_name": "Coral Living Pvt Ltd", "bank_txn_id": "BANK-020", "explicit_fee": "0", "is_bank_charge": "false", "clearing_status": "Pending", "transaction_date": "2026-07-15", "payer_account_number": "", "bank_reference_number": "UTR-COR-020"}	t	\N	04c326170f49853f425702b88378daa5900018135983d5c482dfde21bb40d92d
323da9ea-17d3-4187-849a-280d16a24a88	5d2a38a6-e92b-4dea-887f-f786ed4c5143	\N	89e74967-9b16-4f31-9a1b-f421cee5aafa	\N	\N	UTR-DUPX-015	2026-07-18	\N	\N	\N	\N	Some Vendor Co	\N	\N	INR	150000	150000	\N	CREDIT	0	f	\N	PENDING	f	{"amount": "1500", "narration": "DUPLICATE SUBMISSION TEST", "payer_ifsc": "", "payer_name": "Some Vendor Co", "bank_txn_id": "BANK-015", "explicit_fee": "0", "is_bank_charge": "false", "clearing_status": "Pending", "transaction_date": "2026-07-18", "payer_account_number": "", "bank_reference_number": "UTR-DUPX-015"}	t	\N	5fa0e2284ead95edba861fa36db170b27a78c7e2b5a4f953cbec9af995e5294c
800e7453-659d-45c4-bfb3-5bdba4ec3275	5d2a38a6-e92b-4dea-887f-f786ed4c5143	\N	89e74967-9b16-4f31-9a1b-f421cee5aafa	\N	\N	UTR-DUPX-015	2026-07-18	\N	\N	\N	\N	Some Vendor Co	\N	\N	INR	150000	150000	\N	CREDIT	0	f	\N	PENDING	f	{"amount": "1500", "narration": "DUPLICATE SUBMISSION TEST", "payer_ifsc": "", "payer_name": "Some Vendor Co", "bank_txn_id": "BANK-016", "explicit_fee": "0", "is_bank_charge": "false", "clearing_status": "Pending", "transaction_date": "2026-07-18", "payer_account_number": "", "bank_reference_number": "UTR-DUPX-015"}	t	\N	78af1a4147ae12630f7337f625948f9c32c96ebf277056eb9703071631ba9f2c
d0f0b77b-c182-404c-9f59-47de7795dc89	5d2a38a6-e92b-4dea-887f-f786ed4c5143	\N	89e74967-9b16-4f31-9a1b-f421cee5aafa	\N	\N	FEE-BANK-017	2026-07-20	\N	\N	\N	\N	Bank	\N	\N	INR	50000	50000	\N	CREDIT	0	f	\N	PENDING	f	{"amount": "500", "narration": "MONTHLY ACCOUNT MAINTENANCE FEE", "payer_ifsc": "", "payer_name": "Bank", "bank_txn_id": "BANK-017", "explicit_fee": "0", "is_bank_charge": "true", "clearing_status": "Pending", "transaction_date": "2026-07-20", "payer_account_number": "", "bank_reference_number": "FEE-BANK-017"}	t	\N	7d3afbca17582133a96d3e4946bb179f6243869fb8ee734f4becaa1f360804fd
cdd7e7f7-ea7f-465d-a53a-23806c41a9ed	5d2a38a6-e92b-4dea-887f-f786ed4c5143	\N	89e74967-9b16-4f31-9a1b-f421cee5aafa	\N	\N	UTR-UNK-018	2026-07-21	\N	\N	\N	\N	Unknown Remitter XYZ	\N	\N	INR	99900	99900	\N	CREDIT	0	f	\N	PENDING	f	{"amount": "999", "narration": "MISC TRANSFER UNRECOGNIZED", "payer_ifsc": "", "payer_name": "Unknown Remitter XYZ", "bank_txn_id": "BANK-018", "explicit_fee": "0", "is_bank_charge": "false", "clearing_status": "Pending", "transaction_date": "2026-07-21", "payer_account_number": "", "bank_reference_number": "UTR-UNK-018"}	t	\N	9d8a01ad1393faf9afb0e05a2838151497615b797e27ec16420a8f467a3f2d60
60b6e039-5bfb-4115-ab59-3f613aaebf9b	5d2a38a6-e92b-4dea-887f-f786ed4c5143	\N	9b347616-4920-48e6-a0ff-0d00a2b2b1f7	\N	\N	ACH-883920	2026-06-20	\N	\N	\N	\N	Acme Corp	\N	\N	INR	10000000	10000000	\N	CREDIT	0	f	\N	PENDING	f	{"amount": "100000", "payer_name": "Acme Corp", "bank_txn_id": "BANK-901", "transaction_date": "2026-06-20", "bank_reference_number": "ACH-883920"}	t	\N	fd270599d6702af3cc189873c8af6a9c36514a0f9d7f19c99f5bf771179beb0f
8ed0d60f-7fa1-405f-a149-73d9b64d36c2	5d2a38a6-e92b-4dea-887f-f786ed4c5143	\N	9b347616-4920-48e6-a0ff-0d00a2b2b1f7	\N	\N	LUMP-556677	2026-06-30	\N	\N	\N	\N	Acme Corp	\N	\N	INR	20000000	20000000	\N	CREDIT	0	f	\N	PENDING	f	{"amount": "200000", "payer_name": "Acme Corp", "bank_txn_id": "BANK-906", "transaction_date": "2026-06-30", "bank_reference_number": "LUMP-556677"}	t	\N	b2f37adc79e0a9a2d017ebae95811ac8ee826fa92744cc4ce679c69a4ce55e15
\.


--
-- Data for Name: credit_debit_memos; Type: TABLE DATA; Schema: public; Owner: recon
--

COPY public.credit_debit_memos (memo_id, customer_id, invoice_id, memo_type, memo_date, currency, amount_minor, amount_home_minor, is_open, raw) FROM stdin;
\.


--
-- Data for Name: currencies; Type: TABLE DATA; Schema: public; Owner: recon
--

COPY public.currencies (code, name, minor_unit) FROM stdin;
INR	Indian Rupee	2
USD	US Dollar	2
\.


--
-- Data for Name: customer_bank_accounts; Type: TABLE DATA; Schema: public; Owner: recon
--

COPY public.customer_bank_accounts (account_id, customer_id, bank_account_no, ifsc_code, alias, is_primary, status) FROM stdin;
\.


--
-- Data for Name: customer_reference_codes; Type: TABLE DATA; Schema: public; Owner: recon
--

COPY public.customer_reference_codes (reference_id, customer_id, code_type, code_value, match_priority, is_active) FROM stdin;
\.


--
-- Data for Name: customers; Type: TABLE DATA; Schema: public; Owner: recon
--

COPY public.customers (customer_id, entity_id, customer_code, company_name, pan, gstin, vpa_handle, payment_terms, credit_limit_minor, city, state, created_at, updated_at, raw, source_job_id, valid, issues) FROM stdin;
1798d499-2af5-4362-a804-57a0c9703ab8	5d2a38a6-e92b-4dea-887f-f786ed4c5143	CUST-001	Acme Corp	\N	\N	\N	\N	\N	\N	\N	2026-08-10 11:43:09.299645+00	2026-08-10 11:43:09.299645+00	{"customer_id": "CUST-001", "company_name": "Acme Corp", " expected_utr": " UTR-ADV-2299"}	4d90e3d8-d7bc-4fec-bed2-23c15fe800ef	t	\N
c96200eb-8a0c-402e-b7fd-c30edd06743c	5d2a38a6-e92b-4dea-887f-f786ed4c5143	CUST-002	Apex Logistics	\N	\N	\N	\N	\N	\N	\N	2026-08-10 11:43:09.303544+00	2026-08-10 11:43:09.303544+00	{"customer_id": "CUST-002", "company_name": "Apex Logistics", " expected_utr": null}	4d90e3d8-d7bc-4fec-bed2-23c15fe800ef	t	\N
c0f33fa6-c6ae-43c3-86f7-daf7a532f4b6	5d2a38a6-e92b-4dea-887f-f786ed4c5143	CUST-003	Stark Industries	\N	\N	\N	\N	\N	\N	\N	2026-08-10 11:43:09.306102+00	2026-08-10 11:43:09.306102+00	{"customer_id": "CUST-003", "company_name": "Stark Industries", " expected_utr": null}	4d90e3d8-d7bc-4fec-bed2-23c15fe800ef	t	\N
192608a7-949c-451c-a4b1-cfe7ce4bb8c9	5d2a38a6-e92b-4dea-887f-f786ed4c5143	CUST-004	Globex Inc	\N	\N	\N	\N	\N	\N	\N	2026-08-10 11:43:09.307087+00	2026-08-10 11:43:09.307087+00	{"customer_id": "CUST-004", "company_name": "Globex Inc", " expected_utr": null}	4d90e3d8-d7bc-4fec-bed2-23c15fe800ef	t	\N
e39c4787-d32c-4480-9dbd-84fb8c688942	5d2a38a6-e92b-4dea-887f-f786ed4c5143	CUST-005	Umbrella Corporation	\N	\N	\N	\N	\N	\N	\N	2026-08-10 11:43:09.307725+00	2026-08-10 11:43:09.307725+00	{"customer_id": "CUST-005", "company_name": "Umbrella Corporation", " expected_utr": null}	4d90e3d8-d7bc-4fec-bed2-23c15fe800ef	t	\N
\.


--
-- Data for Name: data_sources; Type: TABLE DATA; Schema: public; Owner: recon
--

COPY public.data_sources (source_id, entity_id, name, kind, status, stream) FROM stdin;
b90e6da1-7058-4202-bf92-32fedcc56200	5d2a38a6-e92b-4dea-887f-f786ed4c5143	Bank Statement	BANK_FEED	CONNECTED	BANK
ff3dcacb-a305-4260-a5d8-57d3311a66d2	5d2a38a6-e92b-4dea-887f-f786ed4c5143	Customers	ERP	CONNECTED	CUSTOMER
223fd495-ca6b-4924-a6d8-0b90247c9377	5d2a38a6-e92b-4dea-887f-f786ed4c5143	Sub-Ledger	ERP	CONNECTED	INVOICE
276128a8-d806-4f07-8cbb-0562f24d9c1e	5d2a38a6-e92b-4dea-887f-f786ed4c5143	General Ledger	ERP	CONNECTED	LEDGER
\.


--
-- Data for Name: documents; Type: TABLE DATA; Schema: public; Owner: recon
--

COPY public.documents (document_id, file_name, byte_size, mime_type, category, storage_uri, linked_type, linked_id, uploaded_by, uploaded_at) FROM stdin;
\.


--
-- Data for Name: entities; Type: TABLE DATA; Schema: public; Owner: recon
--

COPY public.entities (entity_id, organization_id, company_code, name, site_code, home_currency, accounting_standard, created_at, updated_at) FROM stdin;
5d2a38a6-e92b-4dea-887f-f786ed4c5143	e68d45ee-f891-420e-9ed0-1216484b6010	1000	Default Entity	\N	INR	\N	2026-08-10 10:40:03.648253+00	2026-08-10 10:40:03.648253+00
\.


--
-- Data for Name: expected_remittances; Type: TABLE DATA; Schema: public; Owner: recon
--

COPY public.expected_remittances (remittance_id, customer_id, utr_number, declared_amount_minor, currency, declared_date, reconciled, created_at, raw) FROM stdin;
\.


--
-- Data for Name: field_mappings; Type: TABLE DATA; Schema: public; Owner: recon
--

COPY public.field_mappings (mapping_id, version, source_field, canonical_field, transform, transform_param, is_active, stream) FROM stdin;
10a51d13-a282-4e11-9950-ae5f71312861	2	transaction_date	transaction_date	PARSE_DATE	%Y-%m-%d,%d/%m/%Y	f	BANK
d72adae9-9059-4cdb-9ff4-e2f860b9fbd9	2	bank_reference_number	bank_reference	TRIM	\N	f	BANK
0c8a576a-b550-4a23-a038-dcb3e8e8d695	2	payer_name	payer_name	TRIM	\N	f	BANK
d9df73a1-e0b9-4907-aa4c-bf73e6adb414	2	amount	amount_minor	TO_MINOR_UNITS	\N	f	BANK
548059b3-d1a1-40f6-aa38-dd8634df02c5	2	amount	currency	CONST	INR	f	BANK
904ff172-98f0-4271-ac76-ae4a4188da49	2	amount	dr_cr	CONST	CREDIT	f	BANK
6933d99e-a105-4a73-b2d2-b716cfaefe32	3	transaction_date	transaction_date	PARSE_DATE	%Y-%m-%d,%d/%m/%Y	f	BANK
02a09abb-3f0f-4289-ab61-5dd869ad70ef	3	Txn Date	transaction_date	PARSE_DATE	%Y-%m-%d,%d/%m/%Y	f	BANK
00494e4b-7a95-43b8-b57c-f74c93d637db	3	bank_reference_number	bank_reference	TRIM	\N	f	BANK
92c47f25-23e4-4347-af73-e6f2e8bebf0a	3	payer_name	payer_name	TRIM	\N	f	BANK
5fc57da6-3a7b-4399-b742-0476c6116c7d	3	amount	amount_minor	TO_MINOR_UNITS	\N	f	BANK
2b4a2de3-3ea5-42a3-8e63-d6970ceba90e	3	amount	currency	CONST	INR	f	BANK
1419f4ea-ba87-42fd-bd08-2131f77a301d	3	amount	dr_cr	CONST	CREDIT	f	BANK
5d850717-6f76-4661-9c4f-850d4d07aa3c	4	transaction_date	transaction_date	PARSE_DATE	%Y-%m-%d,%d/%m/%Y	t	BANK
bdda6a4b-250a-4eb9-ba99-19b856bb2b34	4	bank_reference_number	bank_reference	TRIM	\N	t	BANK
7b9014f6-843f-46fa-a6c0-617ac470ff36	4	payer_name	payer_name	TRIM	\N	t	BANK
4c4cf5f9-9c04-4eda-bb70-4a0b20c2ac28	4	amount	amount_minor	TO_MINOR_UNITS	\N	t	BANK
84e363ea-32fd-4fdf-8d84-fa10dcf3449c	4	amount	currency	CONST	INR	t	BANK
57a3f4e5-7b94-4cef-a15a-43c771ea3172	4	amount	dr_cr	CONST	CREDIT	t	BANK
b07db200-a5f7-49c9-b21b-fc48ed3b9e97	1	period_date	txn_date	PARSE_DATE	%Y-%m-%d	t	LEDGER
93d59afa-3980-45dc-8246-1601d33782c7	1	gl_balance_id	reference	TRIM	\N	t	LEDGER
58fc7325-9e76-44f4-b3dc-4007d7b3aca9	1	gl_account_code	counterparty	TRIM	\N	t	LEDGER
65191e35-0a5a-4b4a-b794-598800a8e1c1	1	control_account_balance	amount_minor	TO_MINOR_UNITS	\N	t	LEDGER
b7b61c7e-ea48-474e-8426-b283674973f0	1	bank_txn_id	reference	TRIM	\N	f	BANK
efe7dd1f-1a1e-4106-a1b7-2ddfc78d3795	1	transaction_date	txn_date	PARSE_DATE	%Y-%m-%d,%d/%m/%Y	f	BANK
0b2cc740-d86b-4cc0-a015-d93ec2e27484	1	bank_reference	reference	TRIM	\N	f	BANK
5257c219-42e4-4484-aac6-7b04d44d1d97	1	payer_name	counterparty	TRIM	\N	f	BANK
a1f5d071-5bd2-4fe3-9a50-bdef414e224c	1	amount	amount_minor	TO_MINOR_UNITS	\N	f	BANK
f92a9b14-ebfc-4c7d-939e-bff217fe5217	1	explicit_fee	amount_home_minor	TO_MINOR_UNITS	\N	f	BANK
7a8ce505-d321-4e6a-bf04-a4bbacc9b4a5	1	customer_id	reference	TRIM	\N	f	CUSTOMER
eecc852d-f94c-45cc-9384-7b29eded803a	1	company_name	counterparty	TRIM	\N	f	CUSTOMER
2d8ed8ed-7925-4dd4-a0d2-5da9a0e7bcce	1	credit_limit	amount_minor	TO_MINOR_UNITS	\N	f	CUSTOMER
fd47adc2-9db7-4f0f-bca4-431234c7df00	1	currency_code	currency	CONST	INR	f	CUSTOMER
9336509d-c57b-47df-beea-35d962417964	2	customer_id	customer_code	TRIM	\N	t	CUSTOMER
51bf1a0c-5847-4f9f-80bc-86cab0e82bdf	2	company_name	company_name	TRIM	\N	t	CUSTOMER
adcf1f6a-f4dc-476b-925f-9f0e01fccfee	1	issue_date	txn_date	PARSE_DATE	%Y-%m-%d	f	INVOICE
558b112a-c144-468a-aa67-129e84bffa99	1	invoice_number	reference	TRIM	\N	f	INVOICE
bd4b5d39-27ad-4252-98de-bc207e687800	1	customer_id	counterparty	UPPER	\N	f	INVOICE
838435fb-2754-4c0d-9021-1dea3be1a054	1	total_amount	amount_minor	TO_MINOR_UNITS	\N	f	INVOICE
73c85fe0-6822-431a-bc12-49359aed8cdc	2	customer_id	customer_code	TRIM	\N	t	INVOICE
557b63b2-da4b-43b5-8f28-7741822fb493	2	invoice_number	invoice_number	TRIM	\N	t	INVOICE
ebb69e32-245d-4d7e-8643-bae361554ce0	2	issue_date	issue_date	PARSE_DATE	%Y-%m-%d	t	INVOICE
93d014c8-1b08-4896-9a96-0c4db11e6753	2	due_date	due_date	PARSE_DATE	%Y-%m-%d	t	INVOICE
44c7af82-e69c-42de-9f58-7d94c42b5353	2	total_amount	total_amount_minor	TO_MINOR_UNITS	\N	t	INVOICE
8140e876-90b7-4a74-9781-9379e60c383b	2	total_amount	currency	CONST	INR	t	INVOICE
\.


--
-- Data for Name: fx_rates; Type: TABLE DATA; Schema: public; Owner: recon
--

COPY public.fx_rates (fx_rate_id, from_ccy, to_ccy, rate_date, rate, rate_type) FROM stdin;
\.


--
-- Data for Name: gateway_settlements; Type: TABLE DATA; Schema: public; Owner: recon
--

COPY public.gateway_settlements (settlement_id, entity_id, source_job_id, gateway, gateway_transaction_id, customer_id, bank_txn_id, currency, gross_amount_minor, fee_minor, gst_on_fee_minor, net_settled_minor, settlement_date, matched, raw, created_at) FROM stdin;
\.


--
-- Data for Name: gl_accounts; Type: TABLE DATA; Schema: public; Owner: recon
--

COPY public.gl_accounts (gl_account_id, entity_id, account_code, account_name, account_type, normal_balance, l1_group, l2_group, l3_group, is_control) FROM stdin;
\.


--
-- Data for Name: gl_control_balances; Type: TABLE DATA; Schema: public; Owner: recon
--

COPY public.gl_control_balances (balance_id, gl_account_id, period_date, control_balance_minor) FROM stdin;
\.


--
-- Data for Name: gl_journal_entries; Type: TABLE DATA; Schema: public; Owner: recon
--

COPY public.gl_journal_entries (journal_id, entity_id, run_id, posting_date, source_type, memo, posted_by, created_at) FROM stdin;
\.


--
-- Data for Name: gl_journal_lines; Type: TABLE DATA; Schema: public; Owner: recon
--

COPY public.gl_journal_lines (line_id, journal_id, line_number, gl_account_id, dr_cr, currency, amount_minor, amount_home_minor, business_partner_id) FROM stdin;
\.


--
-- Data for Name: immutable_audit_trail; Type: TABLE DATA; Schema: public; Owner: recon
--

COPY public.immutable_audit_trail (audit_id, at, run_id, entry_type, category, action, user_id, target_ref, impact_minor, entity_ref, old_state, new_state, prev_hash, row_hash) FROM stdin;
\.


--
-- Data for Name: ingestion_jobs; Type: TABLE DATA; Schema: public; Owner: recon
--

COPY public.ingestion_jobs (job_id, source_id, file_name, format, trigger_type, row_count, error_count, status, started_by, started_at, locked_by, locked_at, lease_expires_at, attempt_count, max_attempts, next_attempt_at, last_error, file_uri, job_type, parent_job_id, stream, mapping_version, failed_rows, content_hash, unmapped_columns) FROM stdin;
0fcf2fa2-0f40-4f3d-98d2-873d0d733c85	ff3dcacb-a305-4260-a5d8-57d3311a66d2	Customers.csv	CSV	MANUAL	5	0	SUCCESS	\N	2026-08-10 11:21:02.509969+00	\N	2026-08-10 11:21:04.661159+00	\N	1	5	\N	\N	/data/uploads/0fcf2fa2-0f40-4f3d-98d2-873d0d733c85/Customers.csv	INGEST	\N	CUSTOMER	2	\N	\N	\N
6c611abb-3506-42ae-bfe0-ee708a199b0c	ff3dcacb-a305-4260-a5d8-57d3311a66d2	Customers.csv	CSV	MANUAL	5	5	PARTIAL	\N	2026-08-10 11:29:07.221172+00	\N	2026-08-10 11:29:09.556571+00	\N	1	5	\N	\N	/data/uploads/6c611abb-3506-42ae-bfe0-ee708a199b0c/Customers.csv	INGEST	\N	CUSTOMER	2	[{"raw": {"customer_id": "CUST-001", "company_name": "Acme Corp", " expected_utr": " UTR-ADV-2299"}, "issues": ["duplicate key value violates unique constraint \\"customers_entity_id_customer_code_key\\"\\nDETAIL:  Key (entity_id, customer_code)=(5d2a38a6-e92b-4dea-887f-f786ed4c5143, CUST-001) already exists."]}, {"raw": {"customer_id": "CUST-002", "company_name": "Apex Logistics", " expected_utr": null}, "issues": ["duplicate key value violates unique constraint \\"customers_entity_id_customer_code_key\\"\\nDETAIL:  Key (entity_id, customer_code)=(5d2a38a6-e92b-4dea-887f-f786ed4c5143, CUST-002) already exists."]}, {"raw": {"customer_id": "CUST-003", "company_name": "Stark Industries", " expected_utr": null}, "issues": ["duplicate key value violates unique constraint \\"customers_entity_id_customer_code_key\\"\\nDETAIL:  Key (entity_id, customer_code)=(5d2a38a6-e92b-4dea-887f-f786ed4c5143, CUST-003) already exists."]}, {"raw": {"customer_id": "CUST-004", "company_name": "Globex Inc", " expected_utr": null}, "issues": ["duplicate key value violates unique constraint \\"customers_entity_id_customer_code_key\\"\\nDETAIL:  Key (entity_id, customer_code)=(5d2a38a6-e92b-4dea-887f-f786ed4c5143, CUST-004) already exists."]}, {"raw": {"customer_id": "CUST-005", "company_name": "Umbrella Corporation", " expected_utr": null}, "issues": ["duplicate key value violates unique constraint \\"customers_entity_id_customer_code_key\\"\\nDETAIL:  Key (entity_id, customer_code)=(5d2a38a6-e92b-4dea-887f-f786ed4c5143, CUST-005) already exists."]}]	\N	\N
4d90e3d8-d7bc-4fec-bed2-23c15fe800ef	ff3dcacb-a305-4260-a5d8-57d3311a66d2	Customers.csv	CSV	MANUAL	5	0	SUCCESS	\N	2026-08-10 11:43:08.977669+00	\N	2026-08-10 11:43:09.274844+00	\N	1	5	\N	\N	/data/uploads/4d90e3d8-d7bc-4fec-bed2-23c15fe800ef/Customers.csv	INGEST	\N	CUSTOMER	2	\N	\N	\N
89e74967-9b16-4f31-9a1b-f421cee5aafa	b90e6da1-7058-4202-bf92-32fedcc56200	Bank_Statement 2 1.csv	CSV	MANUAL	19	0	SUCCESS	\N	2026-08-11 07:52:39.847033+00	\N	2026-08-11 07:52:41.052732+00	\N	1	5	\N	\N	/data/uploads/89e74967-9b16-4f31-9a1b-f421cee5aafa/Bank_Statement 2 1.csv	INGEST	\N	BANK	2	\N	e30ba09b75e8484c86aa9be323dc9e6064aa2839cfe749aa171d1f77474e03bd	\N
6dc8acee-60e9-48c4-9105-24a3d9fae439	223fd495-ca6b-4924-a6d8-0b90247c9377	SL.csv	CSV	MANUAL	4	0	SUCCESS	\N	2026-08-10 11:56:59.164724+00	\N	2026-08-10 11:57:01.809918+00	\N	1	5	\N	\N	/data/uploads/6dc8acee-60e9-48c4-9105-24a3d9fae439/SL.csv	INGEST	\N	INVOICE	2	\N	\N	\N
569330fb-ea37-4774-a6e7-d8582f5ce838	ff3dcacb-a305-4260-a5d8-57d3311a66d2	Customers.csv	CSV	MANUAL	5	5	PARTIAL	\N	2026-08-10 10:59:57.23309+00	\N	2026-08-10 10:59:59.927001+00	\N	1	5	\N	\N	/data/uploads/569330fb-ea37-4774-a6e7-d8582f5ce838/Customers.csv	INGEST	\N	BANK	2	[{"raw": {"customer_id": "CUST-001", "company_name": "Acme Corp", " expected_utr": " UTR-ADV-2299"}, "issues": ["unknown canonical_field 'customer_code' for stream BANK (ignored)", "unknown canonical_field 'company_name' for stream BANK (ignored)", "missing required field(s): transaction_date, currency, amount_minor, amount_home_minor, dr_cr"]}, {"raw": {"customer_id": "CUST-002", "company_name": "Apex Logistics", " expected_utr": null}, "issues": ["unknown canonical_field 'customer_code' for stream BANK (ignored)", "unknown canonical_field 'company_name' for stream BANK (ignored)", "missing required field(s): transaction_date, currency, amount_minor, amount_home_minor, dr_cr"]}, {"raw": {"customer_id": "CUST-003", "company_name": "Stark Industries", " expected_utr": null}, "issues": ["unknown canonical_field 'customer_code' for stream BANK (ignored)", "unknown canonical_field 'company_name' for stream BANK (ignored)", "missing required field(s): transaction_date, currency, amount_minor, amount_home_minor, dr_cr"]}, {"raw": {"customer_id": "CUST-004", "company_name": "Globex Inc", " expected_utr": null}, "issues": ["unknown canonical_field 'customer_code' for stream BANK (ignored)", "unknown canonical_field 'company_name' for stream BANK (ignored)", "missing required field(s): transaction_date, currency, amount_minor, amount_home_minor, dr_cr"]}, {"raw": {"customer_id": "CUST-005", "company_name": "Umbrella Corporation", " expected_utr": null}, "issues": ["unknown canonical_field 'customer_code' for stream BANK (ignored)", "unknown canonical_field 'company_name' for stream BANK (ignored)", "missing required field(s): transaction_date, currency, amount_minor, amount_home_minor, dr_cr"]}]	\N	\N
9b347616-4920-48e6-a0ff-0d00a2b2b1f7	b90e6da1-7058-4202-bf92-32fedcc56200	Bank_Statement.csv	CSV	MANUAL	3	1	PARTIAL	\N	2026-08-11 09:17:18.844109+00	\N	2026-08-11 09:17:20.595196+00	\N	1	5	\N	\N	/data/uploads/9b347616-4920-48e6-a0ff-0d00a2b2b1f7/Bank_Statement.csv	INGEST	\N	BANK	2	[{"raw": {"amount": "140000", "payer_name": "Apex Logistics", "bank_txn_id": "BANK-902", "transaction_date": "2026-06-29", "bank_reference_number": "NEFT-445566"}, "issues": ["duplicate key value violates unique constraint \\"uniq_bank_statements_row_hash\\"\\nDETAIL:  Key (entity_id, row_hash)=(5d2a38a6-e92b-4dea-887f-f786ed4c5143, 698525ff2d82d63c4a8d26220e698a67149ecc269ceb55ca1fb9b84a6d947fda) already exists."]}]	721332b32c3c2cdfdafa9c42b4d9f64b8ffd608dd4786a5203d857aa67b1d778	{bank_txn_id}
c31d531f-ea9d-4793-86c4-15fe0040c146	ff3dcacb-a305-4260-a5d8-57d3311a66d2	Customers.csv	CSV	MANUAL	5	5	PARTIAL	\N	2026-08-10 11:10:13.137101+00	\N	2026-08-10 11:10:14.185801+00	\N	1	5	\N	\N	/data/uploads/c31d531f-ea9d-4793-86c4-15fe0040c146/Customers.csv	INGEST	\N	BANK	2	[{"raw": {"customer_id": "CUST-001", "company_name": "Acme Corp", " expected_utr": " UTR-ADV-2299"}, "issues": ["unknown canonical_field 'customer_code' for stream BANK (ignored)", "unknown canonical_field 'company_name' for stream BANK (ignored)", "missing required field(s): transaction_date, currency, amount_minor, amount_home_minor, dr_cr"]}, {"raw": {"customer_id": "CUST-002", "company_name": "Apex Logistics", " expected_utr": null}, "issues": ["unknown canonical_field 'customer_code' for stream BANK (ignored)", "unknown canonical_field 'company_name' for stream BANK (ignored)", "missing required field(s): transaction_date, currency, amount_minor, amount_home_minor, dr_cr"]}, {"raw": {"customer_id": "CUST-003", "company_name": "Stark Industries", " expected_utr": null}, "issues": ["unknown canonical_field 'customer_code' for stream BANK (ignored)", "unknown canonical_field 'company_name' for stream BANK (ignored)", "missing required field(s): transaction_date, currency, amount_minor, amount_home_minor, dr_cr"]}, {"raw": {"customer_id": "CUST-004", "company_name": "Globex Inc", " expected_utr": null}, "issues": ["unknown canonical_field 'customer_code' for stream BANK (ignored)", "unknown canonical_field 'company_name' for stream BANK (ignored)", "missing required field(s): transaction_date, currency, amount_minor, amount_home_minor, dr_cr"]}, {"raw": {"customer_id": "CUST-005", "company_name": "Umbrella Corporation", " expected_utr": null}, "issues": ["unknown canonical_field 'customer_code' for stream BANK (ignored)", "unknown canonical_field 'company_name' for stream BANK (ignored)", "missing required field(s): transaction_date, currency, amount_minor, amount_home_minor, dr_cr"]}]	\N	\N
8b40dd5e-d540-4ffb-bd57-af1357aa2379	ff3dcacb-a305-4260-a5d8-57d3311a66d2	Customers.csv	CSV	MANUAL	5	5	PARTIAL	\N	2026-08-10 11:42:21.15436+00	\N	2026-08-10 11:42:24.062455+00	\N	1	5	\N	\N	/data/uploads/8b40dd5e-d540-4ffb-bd57-af1357aa2379/Customers.csv	INGEST	\N	CUSTOMER	2	[{"raw": {"customer_id": "CUST-001", "company_name": "Acme Corp", " expected_utr": " UTR-ADV-2299"}, "issues": ["duplicate key value violates unique constraint \\"customers_entity_id_customer_code_key\\"\\nDETAIL:  Key (entity_id, customer_code)=(5d2a38a6-e92b-4dea-887f-f786ed4c5143, CUST-001) already exists."]}, {"raw": {"customer_id": "CUST-002", "company_name": "Apex Logistics", " expected_utr": null}, "issues": ["duplicate key value violates unique constraint \\"customers_entity_id_customer_code_key\\"\\nDETAIL:  Key (entity_id, customer_code)=(5d2a38a6-e92b-4dea-887f-f786ed4c5143, CUST-002) already exists."]}, {"raw": {"customer_id": "CUST-003", "company_name": "Stark Industries", " expected_utr": null}, "issues": ["duplicate key value violates unique constraint \\"customers_entity_id_customer_code_key\\"\\nDETAIL:  Key (entity_id, customer_code)=(5d2a38a6-e92b-4dea-887f-f786ed4c5143, CUST-003) already exists."]}, {"raw": {"customer_id": "CUST-004", "company_name": "Globex Inc", " expected_utr": null}, "issues": ["duplicate key value violates unique constraint \\"customers_entity_id_customer_code_key\\"\\nDETAIL:  Key (entity_id, customer_code)=(5d2a38a6-e92b-4dea-887f-f786ed4c5143, CUST-004) already exists."]}, {"raw": {"customer_id": "CUST-005", "company_name": "Umbrella Corporation", " expected_utr": null}, "issues": ["duplicate key value violates unique constraint \\"customers_entity_id_customer_code_key\\"\\nDETAIL:  Key (entity_id, customer_code)=(5d2a38a6-e92b-4dea-887f-f786ed4c5143, CUST-005) already exists."]}]	\N	\N
14e8b510-3a90-40be-a374-7cedb925123e	b90e6da1-7058-4202-bf92-32fedcc56200	Bank_Statement.csv	CSV	MANUAL	3	0	SUCCESS	\N	2026-08-11 07:32:15.549755+00	\N	2026-08-11 07:32:17.784342+00	\N	1	5	\N	\N	/data/uploads/14e8b510-3a90-40be-a374-7cedb925123e/Bank_Statement.csv	INGEST	\N	BANK	2	\N	69a1c9f24d00ad17770f07c199dd03d0ba9545e5c0c06e15c5b3f4a1ce5fa7f7	\N
\.


--
-- Data for Name: invitations; Type: TABLE DATA; Schema: public; Owner: recon
--

COPY public.invitations (id, organization_id, email, role_id, token_hash, invited_by_user_id, expires_at, accepted_at, revoked_at, created_at) FROM stdin;
\.


--
-- Data for Name: invoice_allocations; Type: TABLE DATA; Schema: public; Owner: recon
--

COPY public.invoice_allocations (allocation_id, match_group_id, invoice_id, payment_id, bank_txn_id, allocated_minor, gl_journal_id, allocated_at) FROM stdin;
\.


--
-- Data for Name: invoices; Type: TABLE DATA; Schema: public; Owner: recon
--

COPY public.invoices (invoice_id, entity_id, customer_id, invoice_number, issue_date, due_date, currency, total_amount_minor, total_home_minor, balance_due_minor, tds_rate_pct, allowed_tds_minor, status, created_at, updated_at, raw, source_job_id, valid, issues) FROM stdin;
a830b988-6028-47b8-bcd5-6599673a1229	5d2a38a6-e92b-4dea-887f-f786ed4c5143	1798d499-2af5-4362-a804-57a0c9703ab8	INV-2026-001	2026-06-01	2026-07-01	INR	10000000	10000000	10000000	\N	0	OPEN	2026-08-10 11:57:01.837387+00	2026-08-10 11:57:01.837387+00	{"due_date": "2026-07-01", "invoice_id": "INV-101", "issue_date": "2026-06-01", "customer_id": "CUST-001", "total_amount": "100000", "invoice_number": "INV-2026-001"}	6dc8acee-60e9-48c4-9105-24a3d9fae439	t	\N
703346f1-21a6-4789-9c5a-5cdd39614b3a	5d2a38a6-e92b-4dea-887f-f786ed4c5143	c96200eb-8a0c-402e-b7fd-c30edd06743c	INV-2026-002	2026-06-10	2026-06-25	INR	15000000	15000000	15000000	\N	0	OPEN	2026-08-10 11:57:01.839954+00	2026-08-10 11:57:01.839954+00	{"due_date": "2026-06-25", "invoice_id": "INV-102", "issue_date": "2026-06-10", "customer_id": "CUST-002", "total_amount": "150000", "invoice_number": "INV-2026-002"}	6dc8acee-60e9-48c4-9105-24a3d9fae439	t	\N
8f548be3-2b7c-45b9-ae67-9f9428cebd71	5d2a38a6-e92b-4dea-887f-f786ed4c5143	1798d499-2af5-4362-a804-57a0c9703ab8	INV-2026-006	2026-06-22	2026-07-22	INR	7500000	7500000	7500000	\N	0	OPEN	2026-08-10 11:57:01.840739+00	2026-08-10 11:57:01.840739+00	{"due_date": "2026-07-22", "invoice_id": "INV-106", "issue_date": "2026-06-22", "customer_id": "CUST-001", "total_amount": "75000", "invoice_number": "INV-2026-006"}	6dc8acee-60e9-48c4-9105-24a3d9fae439	t	\N
4eb262d0-4458-4686-ac44-80a8db34e00f	5d2a38a6-e92b-4dea-887f-f786ed4c5143	1798d499-2af5-4362-a804-57a0c9703ab8	INV-2026-007	2026-06-24	2026-07-24	INR	12500000	12500000	12500000	\N	0	OPEN	2026-08-10 11:57:01.84149+00	2026-08-10 11:57:01.84149+00	{"due_date": "2026-07-24", "invoice_id": "INV-107", "issue_date": "2026-06-24", "customer_id": "CUST-001", "total_amount": "125000", "invoice_number": "INV-2026-007"}	6dc8acee-60e9-48c4-9105-24a3d9fae439	t	\N
\.


--
-- Data for Name: match_groups; Type: TABLE DATA; Schema: public; Owner: recon
--

COPY public.match_groups (match_group_id, run_id, match_type, rule_id, confidence, status, reason, created_by, created_at) FROM stdin;
\.


--
-- Data for Name: memberships; Type: TABLE DATA; Schema: public; Owner: recon
--

COPY public.memberships (id, user_id, organization_id, role_id, is_active, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: organizations; Type: TABLE DATA; Schema: public; Owner: recon
--

COPY public.organizations (id, name, slug, is_active, created_by_user_id, created_at, updated_at) FROM stdin;
e68d45ee-f891-420e-9ed0-1216484b6010	Recon Platform	recon-platform	t	\N	2026-08-10 10:39:48.334934+00	2026-08-10 10:39:48.334934+00
\.


--
-- Data for Name: payments; Type: TABLE DATA; Schema: public; Owner: recon
--

COPY public.payments (payment_id, bank_txn_id, customer_id, total_received_minor, unapplied_minor, locked_by_rule_id, candidate_pool, created_at) FROM stdin;
\.


--
-- Data for Name: permissions; Type: TABLE DATA; Schema: public; Owner: recon
--

COPY public.permissions (id, code, description) FROM stdin;
\.


--
-- Data for Name: reconciliation_definitions; Type: TABLE DATA; Schema: public; Owner: recon
--

COPY public.reconciliation_definitions (definition_id, entity_id, name, recon_type, cadence, owner_user_id) FROM stdin;
\.


--
-- Data for Name: reconciliation_exceptions; Type: TABLE DATA; Schema: public; Owner: recon
--

COPY public.reconciliation_exceptions (exception_id, run_id, exception_no, exception_type, bank_txn_id, invoice_id, customer_id, discrepancy_minor, reason_code, status, resolution_outcome, resolver_id, resolution_notes, resolved_at, created_at) FROM stdin;
\.


--
-- Data for Name: reconciliation_rules; Type: TABLE DATA; Schema: public; Owner: recon
--

COPY public.reconciliation_rules (rule_id, definition_id, phase, kind, name, priority, enabled, confidence, config) FROM stdin;
\.


--
-- Data for Name: reconciliation_runs; Type: TABLE DATA; Schema: public; Owner: recon
--

COPY public.reconciliation_runs (run_id, definition_id, run_no, period_start, period_end, status, volume, matched_count, exception_count, matched_value_minor, exception_value_minor, unapplied_minor, prepared_by, reviewed_by, signed_at, run_hash, started_at) FROM stdin;
\.


--
-- Data for Name: role_permissions; Type: TABLE DATA; Schema: public; Owner: recon
--

COPY public.role_permissions (role_id, permission_id) FROM stdin;
\.


--
-- Data for Name: roles; Type: TABLE DATA; Schema: public; Owner: recon
--

COPY public.roles (id, name, description, is_system, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: schema_migrations; Type: TABLE DATA; Schema: public; Owner: recon
--

COPY public.schema_migrations (filename, applied_at) FROM stdin;
0001_extensions.sql	2026-08-06 10:20:32.809491+00
0002_auth_users.sql	2026-08-06 10:20:32.814629+00
0003_auth_organizations.sql	2026-08-06 10:20:32.818948+00
0004_auth_roles_permissions.sql	2026-08-06 10:20:32.822687+00
0005_auth_memberships.sql	2026-08-06 10:20:32.825879+00
0006_auth_invitations.sql	2026-08-06 10:20:32.827985+00
0007_auth_sessions.sql	2026-08-06 10:20:32.830374+00
0008_domain_foundation.sql	2026-08-06 10:20:32.832671+00
0009_domain_customers.sql	2026-08-06 10:20:32.839005+00
0010_domain_subledger.sql	2026-08-06 10:20:32.846378+00
0011_domain_data_hub.sql	2026-08-06 10:20:32.851644+00
0012_domain_reconciliation_definitions.sql	2026-08-06 10:20:32.857923+00
0013_domain_bank_and_payments.sql	2026-08-06 10:20:32.863465+00
0014_domain_matching.sql	2026-08-06 10:20:32.871708+00
0015_domain_gl_posting.sql	2026-08-06 10:20:32.876446+00
0016_domain_exceptions.sql	2026-08-06 10:20:32.882389+00
0017_domain_governance.sql	2026-08-06 10:20:32.885437+00
0018_domain_report_views.sql	2026-08-06 10:20:32.889727+00
0019_domain_raw_passthrough.sql	2026-08-06 10:28:29.422532+00
0020_ingestion_worker_support.sql	2026-08-06 11:43:22.634821+00
0021_ingestion_jobs_type_and_stream.sql	2026-08-06 12:21:12.348761+00
0022_direct_to_canonical_ingestion.sql	2026-08-10 08:36:26.858369+00
0023_data_source_stream.sql	2026-08-10 11:40:17.559207+00
0024_data_source_stream_not_null.sql	2026-08-10 11:40:47.774231+00
0025_ingestion_integrity_fixes.sql	2026-08-11 06:49:09.869598+00
0026_global_stream_field_mappings.sql	2026-08-11 09:13:28.581506+00
\.


--
-- Data for Name: sessions; Type: TABLE DATA; Schema: public; Owner: recon
--

COPY public.sessions (id, user_id, organization_id, refresh_token_hash, user_agent, ip_address, issued_at, expires_at, last_used_at, revoked_at) FROM stdin;
\.


--
-- Data for Name: users; Type: TABLE DATA; Schema: public; Owner: recon
--

COPY public.users (id, email, hashed_password, full_name, is_active, is_platform_admin, last_login_at, created_at, updated_at) FROM stdin;
\.


--
-- Name: immutable_audit_trail_audit_id_seq; Type: SEQUENCE SET; Schema: public; Owner: recon
--

SELECT pg_catalog.setval('public.immutable_audit_trail_audit_id_seq', 1, false);


--
-- Name: bank_statements bank_statements_entity_id_document_number_line_number_key; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.bank_statements
    ADD CONSTRAINT bank_statements_entity_id_document_number_line_number_key UNIQUE (entity_id, document_number, line_number);


--
-- Name: bank_statements bank_statements_pkey; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.bank_statements
    ADD CONSTRAINT bank_statements_pkey PRIMARY KEY (bank_txn_id);


--
-- Name: credit_debit_memos credit_debit_memos_pkey; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.credit_debit_memos
    ADD CONSTRAINT credit_debit_memos_pkey PRIMARY KEY (memo_id);


--
-- Name: currencies currencies_pkey; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.currencies
    ADD CONSTRAINT currencies_pkey PRIMARY KEY (code);


--
-- Name: customer_bank_accounts customer_bank_accounts_pkey; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.customer_bank_accounts
    ADD CONSTRAINT customer_bank_accounts_pkey PRIMARY KEY (account_id);


--
-- Name: customer_reference_codes customer_reference_codes_pkey; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.customer_reference_codes
    ADD CONSTRAINT customer_reference_codes_pkey PRIMARY KEY (reference_id);


--
-- Name: customers customers_entity_id_customer_code_key; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.customers
    ADD CONSTRAINT customers_entity_id_customer_code_key UNIQUE (entity_id, customer_code);


--
-- Name: customers customers_pkey; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.customers
    ADD CONSTRAINT customers_pkey PRIMARY KEY (customer_id);


--
-- Name: data_sources data_sources_pkey; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.data_sources
    ADD CONSTRAINT data_sources_pkey PRIMARY KEY (source_id);


--
-- Name: documents documents_pkey; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_pkey PRIMARY KEY (document_id);


--
-- Name: entities entities_organization_id_company_code_key; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.entities
    ADD CONSTRAINT entities_organization_id_company_code_key UNIQUE (organization_id, company_code);


--
-- Name: entities entities_pkey; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.entities
    ADD CONSTRAINT entities_pkey PRIMARY KEY (entity_id);


--
-- Name: expected_remittances expected_remittances_pkey; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.expected_remittances
    ADD CONSTRAINT expected_remittances_pkey PRIMARY KEY (remittance_id);


--
-- Name: field_mappings field_mappings_pkey; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.field_mappings
    ADD CONSTRAINT field_mappings_pkey PRIMARY KEY (mapping_id);


--
-- Name: fx_rates fx_rates_from_ccy_to_ccy_rate_date_rate_type_key; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.fx_rates
    ADD CONSTRAINT fx_rates_from_ccy_to_ccy_rate_date_rate_type_key UNIQUE (from_ccy, to_ccy, rate_date, rate_type);


--
-- Name: fx_rates fx_rates_pkey; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.fx_rates
    ADD CONSTRAINT fx_rates_pkey PRIMARY KEY (fx_rate_id);


--
-- Name: gateway_settlements gateway_settlements_gateway_gateway_transaction_id_key; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.gateway_settlements
    ADD CONSTRAINT gateway_settlements_gateway_gateway_transaction_id_key UNIQUE (gateway, gateway_transaction_id);


--
-- Name: gateway_settlements gateway_settlements_pkey; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.gateway_settlements
    ADD CONSTRAINT gateway_settlements_pkey PRIMARY KEY (settlement_id);


--
-- Name: gl_accounts gl_accounts_entity_id_account_code_key; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.gl_accounts
    ADD CONSTRAINT gl_accounts_entity_id_account_code_key UNIQUE (entity_id, account_code);


--
-- Name: gl_accounts gl_accounts_pkey; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.gl_accounts
    ADD CONSTRAINT gl_accounts_pkey PRIMARY KEY (gl_account_id);


--
-- Name: gl_control_balances gl_control_balances_gl_account_id_period_date_key; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.gl_control_balances
    ADD CONSTRAINT gl_control_balances_gl_account_id_period_date_key UNIQUE (gl_account_id, period_date);


--
-- Name: gl_control_balances gl_control_balances_pkey; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.gl_control_balances
    ADD CONSTRAINT gl_control_balances_pkey PRIMARY KEY (balance_id);


--
-- Name: gl_journal_entries gl_journal_entries_pkey; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.gl_journal_entries
    ADD CONSTRAINT gl_journal_entries_pkey PRIMARY KEY (journal_id);


--
-- Name: gl_journal_lines gl_journal_lines_journal_id_line_number_key; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.gl_journal_lines
    ADD CONSTRAINT gl_journal_lines_journal_id_line_number_key UNIQUE (journal_id, line_number);


--
-- Name: gl_journal_lines gl_journal_lines_pkey; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.gl_journal_lines
    ADD CONSTRAINT gl_journal_lines_pkey PRIMARY KEY (line_id);


--
-- Name: immutable_audit_trail immutable_audit_trail_pkey; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.immutable_audit_trail
    ADD CONSTRAINT immutable_audit_trail_pkey PRIMARY KEY (audit_id);


--
-- Name: ingestion_jobs ingestion_jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.ingestion_jobs
    ADD CONSTRAINT ingestion_jobs_pkey PRIMARY KEY (job_id);


--
-- Name: invitations invitations_pkey; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.invitations
    ADD CONSTRAINT invitations_pkey PRIMARY KEY (id);


--
-- Name: invitations invitations_token_hash_key; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.invitations
    ADD CONSTRAINT invitations_token_hash_key UNIQUE (token_hash);


--
-- Name: invoice_allocations invoice_allocations_match_group_id_invoice_id_payment_id_key; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.invoice_allocations
    ADD CONSTRAINT invoice_allocations_match_group_id_invoice_id_payment_id_key UNIQUE (match_group_id, invoice_id, payment_id);


--
-- Name: invoice_allocations invoice_allocations_pkey; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.invoice_allocations
    ADD CONSTRAINT invoice_allocations_pkey PRIMARY KEY (allocation_id);


--
-- Name: invoices invoices_entity_id_invoice_number_key; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.invoices
    ADD CONSTRAINT invoices_entity_id_invoice_number_key UNIQUE (entity_id, invoice_number);


--
-- Name: invoices invoices_pkey; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.invoices
    ADD CONSTRAINT invoices_pkey PRIMARY KEY (invoice_id);


--
-- Name: match_groups match_groups_pkey; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.match_groups
    ADD CONSTRAINT match_groups_pkey PRIMARY KEY (match_group_id);


--
-- Name: memberships memberships_pkey; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.memberships
    ADD CONSTRAINT memberships_pkey PRIMARY KEY (id);


--
-- Name: memberships memberships_user_id_organization_id_key; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.memberships
    ADD CONSTRAINT memberships_user_id_organization_id_key UNIQUE (user_id, organization_id);


--
-- Name: organizations organizations_pkey; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.organizations
    ADD CONSTRAINT organizations_pkey PRIMARY KEY (id);


--
-- Name: organizations organizations_slug_key; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.organizations
    ADD CONSTRAINT organizations_slug_key UNIQUE (slug);


--
-- Name: payments payments_bank_txn_id_key; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_bank_txn_id_key UNIQUE (bank_txn_id);


--
-- Name: payments payments_pkey; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_pkey PRIMARY KEY (payment_id);


--
-- Name: permissions permissions_code_key; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.permissions
    ADD CONSTRAINT permissions_code_key UNIQUE (code);


--
-- Name: permissions permissions_pkey; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.permissions
    ADD CONSTRAINT permissions_pkey PRIMARY KEY (id);


--
-- Name: reconciliation_definitions reconciliation_definitions_pkey; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.reconciliation_definitions
    ADD CONSTRAINT reconciliation_definitions_pkey PRIMARY KEY (definition_id);


--
-- Name: reconciliation_exceptions reconciliation_exceptions_pkey; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.reconciliation_exceptions
    ADD CONSTRAINT reconciliation_exceptions_pkey PRIMARY KEY (exception_id);


--
-- Name: reconciliation_rules reconciliation_rules_definition_id_phase_priority_key; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.reconciliation_rules
    ADD CONSTRAINT reconciliation_rules_definition_id_phase_priority_key UNIQUE (definition_id, phase, priority);


--
-- Name: reconciliation_rules reconciliation_rules_pkey; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.reconciliation_rules
    ADD CONSTRAINT reconciliation_rules_pkey PRIMARY KEY (rule_id);


--
-- Name: reconciliation_runs reconciliation_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.reconciliation_runs
    ADD CONSTRAINT reconciliation_runs_pkey PRIMARY KEY (run_id);


--
-- Name: reconciliation_runs reconciliation_runs_run_no_key; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.reconciliation_runs
    ADD CONSTRAINT reconciliation_runs_run_no_key UNIQUE (run_no);


--
-- Name: role_permissions role_permissions_pkey; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.role_permissions
    ADD CONSTRAINT role_permissions_pkey PRIMARY KEY (role_id, permission_id);


--
-- Name: roles roles_name_key; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.roles
    ADD CONSTRAINT roles_name_key UNIQUE (name);


--
-- Name: roles roles_pkey; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.roles
    ADD CONSTRAINT roles_pkey PRIMARY KEY (id);


--
-- Name: schema_migrations schema_migrations_pkey; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.schema_migrations
    ADD CONSTRAINT schema_migrations_pkey PRIMARY KEY (filename);


--
-- Name: sessions sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_pkey PRIMARY KEY (id);


--
-- Name: sessions sessions_refresh_token_hash_key; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_refresh_token_hash_key UNIQUE (refresh_token_hash);


--
-- Name: users users_email_key; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_email_key UNIQUE (email);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: idx_alloc_invoice; Type: INDEX; Schema: public; Owner: recon
--

CREATE INDEX idx_alloc_invoice ON public.invoice_allocations USING btree (invoice_id);


--
-- Name: idx_alloc_payment; Type: INDEX; Schema: public; Owner: recon
--

CREATE INDEX idx_alloc_payment ON public.invoice_allocations USING btree (payment_id);


--
-- Name: idx_audit_run; Type: INDEX; Schema: public; Owner: recon
--

CREATE INDEX idx_audit_run ON public.immutable_audit_trail USING btree (run_id, at DESC);


--
-- Name: idx_audit_user; Type: INDEX; Schema: public; Owner: recon
--

CREATE INDEX idx_audit_user ON public.immutable_audit_trail USING btree (user_id, at DESC);


--
-- Name: idx_bank_ref; Type: INDEX; Schema: public; Owner: recon
--

CREATE INDEX idx_bank_ref ON public.bank_statements USING btree (bank_reference);


--
-- Name: idx_bank_status; Type: INDEX; Schema: public; Owner: recon
--

CREATE INDEX idx_bank_status ON public.bank_statements USING btree (recon_status, transaction_date);


--
-- Name: idx_cust_bank_acct; Type: INDEX; Schema: public; Owner: recon
--

CREATE INDEX idx_cust_bank_acct ON public.customer_bank_accounts USING btree (bank_account_no, ifsc_code);


--
-- Name: idx_cust_ref_code; Type: INDEX; Schema: public; Owner: recon
--

CREATE INDEX idx_cust_ref_code ON public.customer_reference_codes USING btree (code_value) WHERE is_active;


--
-- Name: idx_customers_gstin; Type: INDEX; Schema: public; Owner: recon
--

CREATE INDEX idx_customers_gstin ON public.customers USING btree (gstin);


--
-- Name: idx_customers_name; Type: INDEX; Schema: public; Owner: recon
--

CREATE INDEX idx_customers_name ON public.customers USING gin (to_tsvector('simple'::regconfig, company_name));


--
-- Name: idx_exc_customer; Type: INDEX; Schema: public; Owner: recon
--

CREATE INDEX idx_exc_customer ON public.reconciliation_exceptions USING btree (customer_id);


--
-- Name: idx_exc_run; Type: INDEX; Schema: public; Owner: recon
--

CREATE INDEX idx_exc_run ON public.reconciliation_exceptions USING btree (run_id, status, exception_type);


--
-- Name: idx_ingestion_jobs_claimable; Type: INDEX; Schema: public; Owner: recon
--

CREATE INDEX idx_ingestion_jobs_claimable ON public.ingestion_jobs USING btree (status, next_attempt_at);


--
-- Name: idx_ingestion_jobs_content_hash; Type: INDEX; Schema: public; Owner: recon
--

CREATE INDEX idx_ingestion_jobs_content_hash ON public.ingestion_jobs USING btree (source_id, content_hash);


--
-- Name: idx_invoices_open; Type: INDEX; Schema: public; Owner: recon
--

CREATE INDEX idx_invoices_open ON public.invoices USING btree (customer_id, status, due_date) WHERE (status <> 'PAID'::text);


--
-- Name: idx_runs_status; Type: INDEX; Schema: public; Owner: recon
--

CREATE INDEX idx_runs_status ON public.reconciliation_runs USING btree (status, period_end);


--
-- Name: idx_sessions_user; Type: INDEX; Schema: public; Owner: recon
--

CREATE INDEX idx_sessions_user ON public.sessions USING btree (user_id);


--
-- Name: uniq_bank_statements_row_hash; Type: INDEX; Schema: public; Owner: recon
--

CREATE UNIQUE INDEX uniq_bank_statements_row_hash ON public.bank_statements USING btree (entity_id, row_hash) WHERE (row_hash IS NOT NULL);


--
-- Name: uniq_customers_code_ci; Type: INDEX; Schema: public; Owner: recon
--

CREATE UNIQUE INDEX uniq_customers_code_ci ON public.customers USING btree (entity_id, upper(customer_code));


--
-- Name: uniq_reconciled_ref; Type: INDEX; Schema: public; Owner: recon
--

CREATE UNIQUE INDEX uniq_reconciled_ref ON public.bank_statements USING btree (bank_reference) WHERE ((recon_status = 'MATCHED'::text) AND (bank_reference IS NOT NULL));


--
-- Name: bank_statements bank_statements_currency_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.bank_statements
    ADD CONSTRAINT bank_statements_currency_fkey FOREIGN KEY (currency) REFERENCES public.currencies(code);


--
-- Name: bank_statements bank_statements_entity_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.bank_statements
    ADD CONSTRAINT bank_statements_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES public.entities(entity_id);


--
-- Name: bank_statements bank_statements_gl_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.bank_statements
    ADD CONSTRAINT bank_statements_gl_account_id_fkey FOREIGN KEY (gl_account_id) REFERENCES public.gl_accounts(gl_account_id);


--
-- Name: bank_statements bank_statements_source_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.bank_statements
    ADD CONSTRAINT bank_statements_source_job_id_fkey FOREIGN KEY (source_job_id) REFERENCES public.ingestion_jobs(job_id);


--
-- Name: credit_debit_memos credit_debit_memos_currency_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.credit_debit_memos
    ADD CONSTRAINT credit_debit_memos_currency_fkey FOREIGN KEY (currency) REFERENCES public.currencies(code);


--
-- Name: credit_debit_memos credit_debit_memos_customer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.credit_debit_memos
    ADD CONSTRAINT credit_debit_memos_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.customers(customer_id);


--
-- Name: credit_debit_memos credit_debit_memos_invoice_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.credit_debit_memos
    ADD CONSTRAINT credit_debit_memos_invoice_id_fkey FOREIGN KEY (invoice_id) REFERENCES public.invoices(invoice_id);


--
-- Name: customer_bank_accounts customer_bank_accounts_customer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.customer_bank_accounts
    ADD CONSTRAINT customer_bank_accounts_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.customers(customer_id) ON DELETE CASCADE;


--
-- Name: customer_reference_codes customer_reference_codes_customer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.customer_reference_codes
    ADD CONSTRAINT customer_reference_codes_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.customers(customer_id) ON DELETE CASCADE;


--
-- Name: customers customers_entity_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.customers
    ADD CONSTRAINT customers_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES public.entities(entity_id);


--
-- Name: customers customers_source_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.customers
    ADD CONSTRAINT customers_source_job_id_fkey FOREIGN KEY (source_job_id) REFERENCES public.ingestion_jobs(job_id);


--
-- Name: data_sources data_sources_entity_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.data_sources
    ADD CONSTRAINT data_sources_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES public.entities(entity_id);


--
-- Name: documents documents_uploaded_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_uploaded_by_fkey FOREIGN KEY (uploaded_by) REFERENCES public.users(id);


--
-- Name: entities entities_home_currency_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.entities
    ADD CONSTRAINT entities_home_currency_fkey FOREIGN KEY (home_currency) REFERENCES public.currencies(code);


--
-- Name: entities entities_organization_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.entities
    ADD CONSTRAINT entities_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE CASCADE;


--
-- Name: expected_remittances expected_remittances_currency_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.expected_remittances
    ADD CONSTRAINT expected_remittances_currency_fkey FOREIGN KEY (currency) REFERENCES public.currencies(code);


--
-- Name: expected_remittances expected_remittances_customer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.expected_remittances
    ADD CONSTRAINT expected_remittances_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.customers(customer_id);


--
-- Name: invoice_allocations fk_alloc_journal; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.invoice_allocations
    ADD CONSTRAINT fk_alloc_journal FOREIGN KEY (gl_journal_id) REFERENCES public.gl_journal_entries(journal_id);


--
-- Name: fx_rates fx_rates_from_ccy_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.fx_rates
    ADD CONSTRAINT fx_rates_from_ccy_fkey FOREIGN KEY (from_ccy) REFERENCES public.currencies(code);


--
-- Name: fx_rates fx_rates_to_ccy_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.fx_rates
    ADD CONSTRAINT fx_rates_to_ccy_fkey FOREIGN KEY (to_ccy) REFERENCES public.currencies(code);


--
-- Name: gateway_settlements gateway_settlements_bank_txn_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.gateway_settlements
    ADD CONSTRAINT gateway_settlements_bank_txn_id_fkey FOREIGN KEY (bank_txn_id) REFERENCES public.bank_statements(bank_txn_id);


--
-- Name: gateway_settlements gateway_settlements_currency_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.gateway_settlements
    ADD CONSTRAINT gateway_settlements_currency_fkey FOREIGN KEY (currency) REFERENCES public.currencies(code);


--
-- Name: gateway_settlements gateway_settlements_customer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.gateway_settlements
    ADD CONSTRAINT gateway_settlements_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.customers(customer_id);


--
-- Name: gateway_settlements gateway_settlements_entity_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.gateway_settlements
    ADD CONSTRAINT gateway_settlements_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES public.entities(entity_id);


--
-- Name: gateway_settlements gateway_settlements_source_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.gateway_settlements
    ADD CONSTRAINT gateway_settlements_source_job_id_fkey FOREIGN KEY (source_job_id) REFERENCES public.ingestion_jobs(job_id);


--
-- Name: gl_accounts gl_accounts_entity_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.gl_accounts
    ADD CONSTRAINT gl_accounts_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES public.entities(entity_id);


--
-- Name: gl_control_balances gl_control_balances_gl_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.gl_control_balances
    ADD CONSTRAINT gl_control_balances_gl_account_id_fkey FOREIGN KEY (gl_account_id) REFERENCES public.gl_accounts(gl_account_id);


--
-- Name: gl_journal_entries gl_journal_entries_entity_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.gl_journal_entries
    ADD CONSTRAINT gl_journal_entries_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES public.entities(entity_id);


--
-- Name: gl_journal_entries gl_journal_entries_posted_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.gl_journal_entries
    ADD CONSTRAINT gl_journal_entries_posted_by_fkey FOREIGN KEY (posted_by) REFERENCES public.users(id);


--
-- Name: gl_journal_entries gl_journal_entries_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.gl_journal_entries
    ADD CONSTRAINT gl_journal_entries_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.reconciliation_runs(run_id);


--
-- Name: gl_journal_lines gl_journal_lines_business_partner_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.gl_journal_lines
    ADD CONSTRAINT gl_journal_lines_business_partner_id_fkey FOREIGN KEY (business_partner_id) REFERENCES public.customers(customer_id);


--
-- Name: gl_journal_lines gl_journal_lines_currency_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.gl_journal_lines
    ADD CONSTRAINT gl_journal_lines_currency_fkey FOREIGN KEY (currency) REFERENCES public.currencies(code);


--
-- Name: gl_journal_lines gl_journal_lines_gl_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.gl_journal_lines
    ADD CONSTRAINT gl_journal_lines_gl_account_id_fkey FOREIGN KEY (gl_account_id) REFERENCES public.gl_accounts(gl_account_id);


--
-- Name: gl_journal_lines gl_journal_lines_journal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.gl_journal_lines
    ADD CONSTRAINT gl_journal_lines_journal_id_fkey FOREIGN KEY (journal_id) REFERENCES public.gl_journal_entries(journal_id) ON DELETE CASCADE;


--
-- Name: immutable_audit_trail immutable_audit_trail_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.immutable_audit_trail
    ADD CONSTRAINT immutable_audit_trail_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.reconciliation_runs(run_id);


--
-- Name: immutable_audit_trail immutable_audit_trail_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.immutable_audit_trail
    ADD CONSTRAINT immutable_audit_trail_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);


--
-- Name: ingestion_jobs ingestion_jobs_parent_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.ingestion_jobs
    ADD CONSTRAINT ingestion_jobs_parent_job_id_fkey FOREIGN KEY (parent_job_id) REFERENCES public.ingestion_jobs(job_id);


--
-- Name: ingestion_jobs ingestion_jobs_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.ingestion_jobs
    ADD CONSTRAINT ingestion_jobs_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.data_sources(source_id);


--
-- Name: ingestion_jobs ingestion_jobs_started_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.ingestion_jobs
    ADD CONSTRAINT ingestion_jobs_started_by_fkey FOREIGN KEY (started_by) REFERENCES public.users(id);


--
-- Name: invitations invitations_invited_by_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.invitations
    ADD CONSTRAINT invitations_invited_by_user_id_fkey FOREIGN KEY (invited_by_user_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: invitations invitations_organization_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.invitations
    ADD CONSTRAINT invitations_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE CASCADE;


--
-- Name: invitations invitations_role_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.invitations
    ADD CONSTRAINT invitations_role_id_fkey FOREIGN KEY (role_id) REFERENCES public.roles(id) ON DELETE RESTRICT;


--
-- Name: invoice_allocations invoice_allocations_bank_txn_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.invoice_allocations
    ADD CONSTRAINT invoice_allocations_bank_txn_id_fkey FOREIGN KEY (bank_txn_id) REFERENCES public.bank_statements(bank_txn_id);


--
-- Name: invoice_allocations invoice_allocations_invoice_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.invoice_allocations
    ADD CONSTRAINT invoice_allocations_invoice_id_fkey FOREIGN KEY (invoice_id) REFERENCES public.invoices(invoice_id);


--
-- Name: invoice_allocations invoice_allocations_match_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.invoice_allocations
    ADD CONSTRAINT invoice_allocations_match_group_id_fkey FOREIGN KEY (match_group_id) REFERENCES public.match_groups(match_group_id) ON DELETE CASCADE;


--
-- Name: invoice_allocations invoice_allocations_payment_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.invoice_allocations
    ADD CONSTRAINT invoice_allocations_payment_id_fkey FOREIGN KEY (payment_id) REFERENCES public.payments(payment_id);


--
-- Name: invoices invoices_currency_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.invoices
    ADD CONSTRAINT invoices_currency_fkey FOREIGN KEY (currency) REFERENCES public.currencies(code);


--
-- Name: invoices invoices_customer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.invoices
    ADD CONSTRAINT invoices_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.customers(customer_id);


--
-- Name: invoices invoices_entity_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.invoices
    ADD CONSTRAINT invoices_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES public.entities(entity_id);


--
-- Name: invoices invoices_source_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.invoices
    ADD CONSTRAINT invoices_source_job_id_fkey FOREIGN KEY (source_job_id) REFERENCES public.ingestion_jobs(job_id);


--
-- Name: match_groups match_groups_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.match_groups
    ADD CONSTRAINT match_groups_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id);


--
-- Name: match_groups match_groups_rule_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.match_groups
    ADD CONSTRAINT match_groups_rule_id_fkey FOREIGN KEY (rule_id) REFERENCES public.reconciliation_rules(rule_id);


--
-- Name: match_groups match_groups_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.match_groups
    ADD CONSTRAINT match_groups_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.reconciliation_runs(run_id) ON DELETE CASCADE;


--
-- Name: memberships memberships_organization_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.memberships
    ADD CONSTRAINT memberships_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE CASCADE;


--
-- Name: memberships memberships_role_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.memberships
    ADD CONSTRAINT memberships_role_id_fkey FOREIGN KEY (role_id) REFERENCES public.roles(id) ON DELETE RESTRICT;


--
-- Name: memberships memberships_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.memberships
    ADD CONSTRAINT memberships_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: organizations organizations_created_by_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.organizations
    ADD CONSTRAINT organizations_created_by_user_id_fkey FOREIGN KEY (created_by_user_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: payments payments_bank_txn_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_bank_txn_id_fkey FOREIGN KEY (bank_txn_id) REFERENCES public.bank_statements(bank_txn_id);


--
-- Name: payments payments_customer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.customers(customer_id);


--
-- Name: payments payments_locked_by_rule_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_locked_by_rule_id_fkey FOREIGN KEY (locked_by_rule_id) REFERENCES public.reconciliation_rules(rule_id);


--
-- Name: reconciliation_definitions reconciliation_definitions_entity_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.reconciliation_definitions
    ADD CONSTRAINT reconciliation_definitions_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES public.entities(entity_id);


--
-- Name: reconciliation_definitions reconciliation_definitions_owner_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.reconciliation_definitions
    ADD CONSTRAINT reconciliation_definitions_owner_user_id_fkey FOREIGN KEY (owner_user_id) REFERENCES public.users(id);


--
-- Name: reconciliation_exceptions reconciliation_exceptions_bank_txn_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.reconciliation_exceptions
    ADD CONSTRAINT reconciliation_exceptions_bank_txn_id_fkey FOREIGN KEY (bank_txn_id) REFERENCES public.bank_statements(bank_txn_id);


--
-- Name: reconciliation_exceptions reconciliation_exceptions_customer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.reconciliation_exceptions
    ADD CONSTRAINT reconciliation_exceptions_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.customers(customer_id);


--
-- Name: reconciliation_exceptions reconciliation_exceptions_invoice_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.reconciliation_exceptions
    ADD CONSTRAINT reconciliation_exceptions_invoice_id_fkey FOREIGN KEY (invoice_id) REFERENCES public.invoices(invoice_id);


--
-- Name: reconciliation_exceptions reconciliation_exceptions_resolver_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.reconciliation_exceptions
    ADD CONSTRAINT reconciliation_exceptions_resolver_id_fkey FOREIGN KEY (resolver_id) REFERENCES public.users(id);


--
-- Name: reconciliation_exceptions reconciliation_exceptions_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.reconciliation_exceptions
    ADD CONSTRAINT reconciliation_exceptions_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.reconciliation_runs(run_id) ON DELETE CASCADE;


--
-- Name: reconciliation_rules reconciliation_rules_definition_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.reconciliation_rules
    ADD CONSTRAINT reconciliation_rules_definition_id_fkey FOREIGN KEY (definition_id) REFERENCES public.reconciliation_definitions(definition_id) ON DELETE CASCADE;


--
-- Name: reconciliation_runs reconciliation_runs_definition_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.reconciliation_runs
    ADD CONSTRAINT reconciliation_runs_definition_id_fkey FOREIGN KEY (definition_id) REFERENCES public.reconciliation_definitions(definition_id);


--
-- Name: reconciliation_runs reconciliation_runs_prepared_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.reconciliation_runs
    ADD CONSTRAINT reconciliation_runs_prepared_by_fkey FOREIGN KEY (prepared_by) REFERENCES public.users(id);


--
-- Name: reconciliation_runs reconciliation_runs_reviewed_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.reconciliation_runs
    ADD CONSTRAINT reconciliation_runs_reviewed_by_fkey FOREIGN KEY (reviewed_by) REFERENCES public.users(id);


--
-- Name: role_permissions role_permissions_permission_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.role_permissions
    ADD CONSTRAINT role_permissions_permission_id_fkey FOREIGN KEY (permission_id) REFERENCES public.permissions(id) ON DELETE CASCADE;


--
-- Name: role_permissions role_permissions_role_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.role_permissions
    ADD CONSTRAINT role_permissions_role_id_fkey FOREIGN KEY (role_id) REFERENCES public.roles(id) ON DELETE CASCADE;


--
-- Name: sessions sessions_organization_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE CASCADE;


--
-- Name: sessions sessions_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: recon
--

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--

\unrestrict iW2p02cAI7yi90cMacfFnWm9SMEThdmOcCU7g68nMHqrNIMXyNwW0ulT2ymo7ex

