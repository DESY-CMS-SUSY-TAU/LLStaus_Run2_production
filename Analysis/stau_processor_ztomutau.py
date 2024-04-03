# This file illustrates how to implement a processor, realizing the selection
# steps and outputting histograms and a cutflow with efficiencies.
# Here we create a very simplified version of the ttbar-to-dilep processor.
# One can run this processor using
# 'python3 -m pepper.runproc --debug example_processor.py example_config.json'
# Above command probably will need a little bit of time before all cuts are
# applied once. This is because a chunk of events are processed simultaneously.
# You change adjust the number of events in a chunk and thereby the memory
from nis import match
from unittest import result
import pepper
import awkward as ak
from functools import partial
import numpy as np
import mt2
import numba as nb
import coffea

from coffea.nanoevents import NanoAODSchema
from functools import partial

class Processor(pepper.ProcessorBasicPhysics):
    # We use the ConfigTTbarLL instead of its base Config, to use some of its
    # predefined extras
    config_class = pepper.ConfigSTau
    
    def zero_handler(func):
        def _function(self, data, *args, **kwargs):
            if len(data) > 0: return func(self, data, *args, **kwargs)
            else: return ak.Array([])
        return _function
    
    def __init__(self, config, eventdir):
        # Initialize the class, maybe overwrite some config variables and
        # load additional files if needed
        # Can set and modify configuration here as well
        config["histogram_format"] = "root"
        # Need to call parent init to make histograms and such ready
        super().__init__(config, eventdir)

        if "pileup_reweighting" not in config:
            logger.warning("No pileup reweigthing specified")

        if "DY_lo_sfs" not in config:
            logger.warning("No DY k-factor specified")

        if "DY_jet_reweight" not in config or len(config["DY_jet_reweight"]) == 0:
            logger.warning("No DY jet-binned sample stitching is specified")

        if "muon_sf" not in config or len(config["muon_sf"]) == 0:
            logger.warning("No muon scale factors specified")

        if "single_mu_trigger_sfs" not in config or\
            len(config["single_mu_trigger_sfs"]) == 0:
            logger.warning("No single muon trigger scale factors specified")
        
        # if "jet_fake_rate" not in config or len(config["jet_fake_rate"]) == 0:
        #     self.predict_jet_fakes = False
        #     logger.warning("No jet fake rate specified")
        # else:
        #     self.predict_jet_fakes = config["predict_yield"]

    def process_selection(self, selector, dsname, is_mc, filler):
        
        # Triggers
        pos_triggers, neg_triggers = pepper.misc.get_trigger_paths_for(
            dsname,
            is_mc,
            self.config["dataset_trigger_map"],
            self.config["dataset_trigger_order"])
        selector.add_cut("Trigger", partial(
            self.passing_trigger, pos_triggers, neg_triggers))
        
        if is_mc and ( dsname.startswith("DYJetsToLL_M-50_TuneCP5_13TeV-madgraphMLM-pythia8") or \
                       dsname.startswith("DY1JetsToLL_M-50_MatchEWPDG20_TuneCP5_13TeV-madgraphMLM-pythia8") or \
                       dsname.startswith("DY2JetsToLL_M-50_MatchEWPDG20_TuneCP5_13TeV-madgraphMLM-pythia8") or \
                       dsname.startswith("DY3JetsToLL_M-50_MatchEWPDG20_TuneCP5_13TeV-madgraphMLM-pythia8") or \
                       dsname.startswith("DY4JetsToLL_M-50_MatchEWPDG20_TuneCP5_13TeV-madgraphMLM-pythia8") ):
            selector.add_cut("DY jet reweighting",
                partial(self.do_dy_jet_reweighting))

        if is_mc and "pileup_reweighting" in self.config:
            selector.add_cut("Pileup reweighting", partial(
                self.do_pileup_reweighting, dsname))

        # HEM 15/16 failure (2018)
        if self.config["year"] == "ul2018":
            selector.add_cut("HEM_veto", partial(self.HEM_veto, is_mc=is_mc))

        # PV cut
        selector.add_cut("PV", lambda data: data["PV"].npvsGood > 0)
        
        # MET cut
        # selector.add_cut("MET", self.MET_cut_max)
        selector.add_cut("MET filters", partial(self.met_filters, is_mc))

        # MET cut
        # selector.add_cut("MET", self.MET_cut_max)
        
        # 2 muons
        selector.set_column("Muon_tag", self.select_muons)
        selector.set_column("Tau", self.select_taus)

        selector.add_cut("one_muon_cut", self.one_muon_cut)
        selector.set_cat("sideband", {"iso", "antiiso"})
        selector.set_multiple_columns(self.isolation_sideband)

        selector.add_cut("more_one_tau_cut", self.one_tau_cut)

        # Select veto objects before modifying Muon collection
        selector.set_column("muon_veto", self.muons_veto)
        selector.set_column("electron_veto", self.electron_veto)
        
        # veto all loose muons
        selector.add_cut("loose_muon_veto", self.loose_muon_veto_cut)
        # veto all loose electrons
        selector.add_cut("loose_electron_veto", self.loose_electron_veto_cut)

        selector.set_column("sum_ll", self.sum_mutau)
        if is_mc:
            selector.set_column("sum_ll_gen", self.sum_ll_gen)

        # selector.add_cut("dy_gen_sfs", partial(self.get_dy_gen_sfs, is_mc=is_mc, dsname=dsname))
        selector.add_cut("muon_sfs", partial(self.get_muon_sfs, is_mc=is_mc))

        selector.add_cut("mass_window", self.mass_window)
        selector.set_column("dphi_mutau", self.dphi)
        selector.add_cut("dphi_min_cut", self.dphi_min_cut)

        # calculate dzeta from muon and tau
        selector.set_column("dzeta", self.dzeta)
        selector.set_column("muon_mt", self.muon_mt)
        
        selector.add_cut("dzeta_cut", self.dzeta_cut)
        selector.add_cut("muon_mt_cut", self.muon_mt_cut)

        selector.set_cat("control_region", {"SS", "OS"})
        selector.set_multiple_columns(self.op_charge)
        
        selector.set_column("Jet_select", self.jet_selection)
        selector.set_column("PfCands", self.pfcand_valid)
        selector.set_column("Jet_lead_pfcand", partial(self.get_matched_pfCands, match_object="Jet_select", dR=0.4))
        selector.set_column("Jet_select", self.set_jet_dxy)
        selector.add_cut("has_jet", self.has_jet)
        selector.set_column("Jet_select_lead", self.jet_selection_lead)
        
        # seperate regions by the gen-level tau matching
        selector.set_cat("gen_match", {"gen-tau", "gen-other", "gen-all"})
        selector.set_multiple_columns(self.gen_matching)
        
        selector.set_column("tauID_SFs", partial(self.apply_tauID_SFs, is_mc=is_mc))
        
        # Tagger part for calculating scale factors
        # Scale factors should be calculated -
        # before cuts on the number of the jets
        # selector.set_multiple_columns(self.set_njets_pass)
        # if self.config["predict_yield"]:
        #     selector.set_multiple_columns(partial(self.predict_yield, weight=selector.systematics["weight"]))
        
        selector.add_cut("final_state", self.mass_window)

    @zero_handler
    def has_jet(self, data):
        return ak.num(data["Jet_select"]) > 0
    
    @zero_handler
    def dzeta(self, data):
        muon = ak.firsts(data["Muon_tag"])
        tau = ak.firsts(data["Tau"])
        met = data["MET"]
        leg1, leg2 = ak.zip({"x": muon.px, "y": muon.py}), ak.zip({"x": tau.px, "y": tau.py})
        zetaAxis = ak.zip({
            "x": (leg1.x/np.sqrt(leg1.x**2 + leg1.y**2) + leg2.x/np.sqrt(leg2.x**2 + leg2.y**2)),
            "y": (leg1.y/np.sqrt(leg1.x**2 + leg1.y**2) + leg2.y/np.sqrt(leg2.x**2 + leg2.y**2))
        })
        norm = np.sqrt(zetaAxis.x**2 + zetaAxis.y**2)
        zetaAxis = ak.zip({
            "x": zetaAxis.x / norm,
            "y": zetaAxis.y / norm
        })
        # Calculate pzetavis as the dot product of (leg1 + leg2) with zetaAxis
        pzetavis = (leg1.x + leg2.x) * zetaAxis.x + (leg1.y + leg2.y) * zetaAxis.y
        # Assuming met is structured as ak.Array with fields .px and .py
        met_vect = ak.zip({"x": met.px, "y": met.py})
        # Calculate pzetamiss as the dot product of met with zetaAxis
        pzetamiss = met_vect.x * zetaAxis.x + met_vect.y * zetaAxis.y
        # Calculate dzeta
        dzeta = pzetamiss - 0.85 * pzetavis
        return dzeta

    @zero_handler
    def muon_mt(self, data):
        muon = ak.firsts(data["Muon_tag"])
        met = data["MET"]
        mt = np.sqrt(2 * muon.pt * met.pt * (1 - np.cos(muon.delta_phi(met))))
        return mt

    @zero_handler
    def dzeta_cut(self, data):
        return data["dzeta"] > self.config["dzeta_cut"]
    
    @zero_handler
    def muon_mt_cut(self, data):
        return data["muon_mt"] < self.config["muon_mt_cut"]

    def gen_matching(self, data):
        if len(data) == 0:
            return {
                "gen-tau" : ak.Array([]),
                "gen-other" : ak.Array([]),
                "gen-all" : ak.Array([])
            }
        tau = ak.firsts(data["Tau"])
        is_muon = (abs(tau.genPartFlav) == 2) | (abs(tau.genPartFlav) == 4)
        is_elec = (abs(tau.genPartFlav) == 1) | (abs(tau.genPartFlav) == 3)
        is_tau = (abs(tau.genPartFlav) == 5)
        is_other = (abs(tau.genPartFlav) == 0)
        return {
            "gen-tau" : is_tau,
            "gen-other" : (is_muon | is_elec | is_other),
            "gen-all" : ak.Array([True]*len(data))
        }
    
    @zero_handler
    def apply_tauID_SFs(self, data, is_mc):
        weight = np.ones(len(data))
        if is_mc:
            tau = ak.firsts(data["Tau"])
            gen_tau_events = data["gen-tau"]
            tau_id_sf = self.config["tauid_sfs"](tau_pt = tau[gen_tau_events].pt, 
                                                 tau_dm = -999, 
                                                 genmatch = 5,
                                                 syst = "nom"
                                                 )
            weight[gen_tau_events] = tau_id_sf
        return weight
    
    # @zero_handler
    # def MET_cut_max(self, data):
    #     return data["MET"].pt < self.config["MET_cut_max"]
    
    def isolation_sideband(self, data):
        if len(data) == 0:
            return {
                "iso" : ak.Array([]),
                "antiiso" : ak.Array([])
            }
        muon = ak.firsts(data["Muon_tag"])
        iso = (muon.pfRelIso04_all <= self.config["muon_pfRelIso04_all"])
        antiiso = (muon.pfRelIso04_all > self.config["muon_pfRelIso04_all"])
        return {
            "iso" : iso,
            "antiiso" : antiiso
        }
    
    @zero_handler
    def do_dy_jet_reweighting(self, data):
        njet = data["LHE"]["Njets"]
        weights = self.config["DY_jet_reweight"][njet]
        return weights
        
    def delta_phi(self, phi1_ak, phi2_ak):
        phi1 = np.array(phi1_ak)
        phi2 = np.array(phi2_ak)
        assert phi1.shape == phi2.shape
        d = phi1 - phi2
        indx_pos = d>np.pi
        d[indx_pos] -= np.pi*2
        indx_neg = d<=-np.pi
        d[indx_neg] += np.pi*2
        return d
    
    @zero_handler
    def dphi(self, data):
        muon = ak.firsts(data["Muon_tag"])
        tau = ak.firsts(data["Tau"])
        return self.delta_phi(muon.phi, tau.phi)

    @zero_handler
    def dphi_min_cut(self, data):
        return abs(data["dphi_mutau"]) > self.config["dphi_ll_min"]

    @zero_handler
    def HEM_veto(self, data, is_mc):
        weight = np.ones(len(data), dtype=np.float32)
        jets = data["Jet"]
        elctron = data["Electron"]
        electron_in15or16_hem = ( (elctron.pt > 20) & (elctron.eta > -3.0) & (elctron.eta < -1.3) & (elctron.phi > -1.57) & (elctron.phi < -0.87) )
        jet_in15or16_hem = ( (jets.pt > 20) & (jets.eta > -3.2) & (jets.eta < -1.3) & (jets.phi > -1.77) & (jets.phi < -0.67) )
        in_hem = (ak.any(electron_in15or16_hem, axis=-1) | ak.any(jet_in15or16_hem, axis=-1))
        if is_mc:
            weight[in_hem] = (1-0.66)
        else:
            issue_period = (data.run >= 319077)
            weight[in_hem & issue_period] = 0.0
        return weight  

    @zero_handler
    def select_muons(self, data):
        muons = data["Muon"]
        is_good = (
              (muons.pt > self.config["muon_pt_min"])
            & (muons.eta < self.config["muon_eta_max"])
            & (muons.eta > self.config["muon_eta_min"])
            & (muons[self.config["muon_ID"]] == 1)
            # & (muons.pfRelIso04_all < self.config["muon_pfRelIso04_all"])
            & (muons.pfRelIso04_all < self.config["muon_pfRelIso04_all_anti"])
            & (abs(muons.dxy) <= self.config["muon_absdxy"])
            & (abs(muons.dz) <= self.config["muon_absdz"])
            )
        muons = muons[is_good]
        
        trig_muons = data["TrigObj"]
        trig_muons = trig_muons[
              (abs(trig_muons.id) == 13)
            & (trig_muons.filterBits >= 8)
            ]
        
        matches, dRlist = muons.nearest(trig_muons, return_metric=True, threshold=0.4)
        idx_matches_muon = ~ak.is_none(matches, axis=1)
        results = muons[idx_matches_muon]
        
        return results
    
    @zero_handler
    def select_taus(self, data):
        taus = data["Tau"]
        is_good = (
            (taus.pt > self.config["tau_pt_min"])
            & (taus.eta < self.config["tau_eta_max"])
            & (taus.eta > self.config["tau_eta_min"])
            & (taus.idDeepTau2017v2p1VSjet >= self.config["tau_idDeepTau_vsjet"])
            & (taus.idDeepTau2017v2p1VSmu >= self.config["tau_idDeepTau_vsmu"])
            & (taus.idDeepTau2017v2p1VSe >= self.config["tau_idDeepTau_vsele"])
            & (abs(taus.charge) == 1)
            # & (abs(taus.dxy) <= self.config["tau_absdxy"])
            # & (abs(taus.dz) <= self.config["tau_absdz"])
        )
        return taus[is_good]
    
    @zero_handler
    def muons_veto(self, data):
        muons = data["Muon"]
        is_good = (
              (muons.pt > self.config["muon_veto_pt_min"])
            & (muons.eta < self.config["muon_veto_eta_max"])
            & (muons.eta > self.config["muon_veto_eta_min"])
            & (muons[self.config["muon_veto_ID"]] == 1)
            & (muons.pfIsoId >= self.config["muon_veto_pfIsoId"])
            )
        muons = muons[is_good]
        # Remove trigger matched muons
        tag_muons = data["Muon_tag"]
        matches_h, dRlist = muons.nearest(tag_muons, return_metric=True, threshold=0.4)
        muons = muons[ak.is_none(matches_h, axis=-1)]
        return muons
        
    @zero_handler
    def electron_veto(self, data):
        ele = data["Electron"]
        is_good = (
            (ele.pt > self.config["elec_veto_pt"])
            & (ele.eta > self.config["elec_veto_eta_min"])
            & (ele.eta < self.config["elec_veto_eta_max"])
            & (ele[self.config["elec_veto"]] == 1)
            & (ele[self.config["elec_ID"]] == 1)
            )
        return ele[is_good]

    @zero_handler
    def more_one_muon_cut(self, data):
        # Exactly one tag muon
        return ak.num(data["Muon_tag"]) >= 1
    
    @zero_handler
    def one_muon_cut(self, data):
        # Exactly one tag muon
        return ak.num(data["Muon_tag"]) == 1
    
    @zero_handler
    def one_tau_cut(self, data):
        return ak.num(data["Tau"]) >= 1
    
    @zero_handler
    def loose_muon_veto_cut(self, data):
        return ak.num(data["muon_veto"])==0
    
    @zero_handler
    def loose_electron_veto_cut(self, data):
        return ak.num(data["electron_veto"])==0
    
    @zero_handler
    def sum_mutau(self, data):
        muon = ak.firsts(data["Muon_tag"])
        tau = ak.firsts(data["Tau"])
        return muon.add(tau)
    
    @zero_handler
    def sum_ll_gen(self, data):
        part = data["GenPart"]
        part = part[ part.hasFlags("isLastCopy")
                & (part.hasFlags("fromHardProcess")
                & ((abs(part["pdgId"]) == 11) 
                | (abs(part["pdgId"]) == 13)
                | (abs(part["pdgId"]) == 12)
                | (abs(part["pdgId"]) == 14)
                | (abs(part["pdgId"]) == 15)
                | (abs(part["pdgId"]) == 16)))
                | (part.hasFlags("isDirectHardProcessTauDecayProduct"))
        ]
        sum_p4 = part.sum(axis=1) # sum over all particles in event
        return sum_p4

    @zero_handler
    def get_dy_gen_sfs(self, data, is_mc, dsname):
        weight = np.ones(len(data))
        if is_mc and dsname.startswith("DY"):
            z_boson = data["sum_ll_gen"]
            dy_gen_sfs = self.config["DY_lo_sfs"](mass=z_boson.mass, pt=z_boson.pt)
            weight *= ak.to_numpy(dy_gen_sfs)
            return weight
        else:
            return weight

    @zero_handler
    def mass_window(self, data):
        is_good = (
              (data["sum_ll"].mass > self.config["mass_ll_lower"])
            & (data["sum_ll"].mass < self.config["mass_ll_upper"])
            )
        return is_good
    
    def op_charge(self, data):
        if len(data) == 0:
            return {
                "OS" : ak.Array([]),
                "SS" : ak.Array([])
            }
        return {
            "OS" : data["sum_ll"].charge == 0,
            "SS" : data["sum_ll"].charge != 0
        }
    
    @zero_handler
    def get_muon_sfs(self, data, is_mc):
        weight = np.ones(len(data))
        if is_mc:
            id_iso_sfs, systematics = self.muon_id_iso_sfs(data)
            weight *= ak.to_numpy(id_iso_sfs)
            mu_trigger_sfs = self.apply_mu_trigger_sfs(data) # systematics are not implemented yet
            weight *= ak.to_numpy(mu_trigger_sfs)
            return weight, systematics
        else:
            return weight

    def apply_mu_trigger_sfs(self, data):
        weight = np.ones(len(data))
        # Single muon trigger efficiency applied for leading muon
        muon = ak.firsts(data["Muon_tag"])
        sfs = self.config["single_mu_trigger_sfs"][0](pt=muon.pt, abseta=abs(muon.eta))
        return weight * sfs

    def muon_id_iso_sfs(self, data):
        """Compute identification and isolation scale factors for
           leptons (electrons and muons)."""
        muons = data["Muon_tag"]
        weight = np.ones(len(data))
        systematics = {}
        # Muon identification and isolation efficiency
        for i, sffunc in enumerate(self.config["muon_sf"]):
            params = {}
            for dimlabel in sffunc.dimlabels:
                if dimlabel == "abseta":
                    params["abseta"] = abs(muons.eta)
                else:
                    params[dimlabel] = getattr(muons, dimlabel)
            central = ak.prod(sffunc(**params), axis=1)
            key = f"muonsf{i}"
            if self.config["compute_systematics"]:
                if ("split_muon_uncertainty" not in self.config
                        or not self.config["split_muon_uncertainty"]):
                    unctypes = ("",)
                else:
                    unctypes = ("stat ", "syst ")
                for unctype in unctypes:
                    up = ak.prod(sffunc(
                        **params, variation=f"{unctype}up"), axis=1)
                    down = ak.prod(sffunc(
                        **params, variation=f"{unctype}down"), axis=1)
                    systematics[key + unctype.replace(" ", "")] = (
                        up / central, down / central)
            weight = weight * central
        return weight, systematics

    @zero_handler
    def jet_selection(self, data):
        jets = data["Jet"]
        jets = jets[(
            (self.config["jet_eta_min"] < jets.eta)
            & (jets.eta < self.config["jet_eta_max"])
            & (self.config["jet_pt_min"] < jets.pt)
            # & (jets.jetId >= self.config["jet_jetId"] )
            )]
        tau = data["Tau"]
        matches_h, dRlist = tau.nearest(jets, return_metric=True, threshold=self.config["jet_dr_tau"])
        taujets = matches_h[~ak.is_none(matches_h, axis=1)]
        return taujets
    
    @zero_handler
    def pfcand_valid(self, data):
        pfCands = data["PFCandidate"]
        is_good = (
            (pfCands.pt > self.config["pfcand_pt"])
            & (pfCands.eta < self.config["pfcand_eta_max"])
            & (pfCands.eta > self.config["pfcand_eta_min"])
            & (pfCands[self.config["track"]])
        )
        pfCands_selected = pfCands[is_good]
        sort_idx = ak.argsort(pfCands_selected.pt, axis=-1, ascending=False)
        return pfCands_selected[sort_idx]
    
    @zero_handler
    def match_jet_to_pfcand(self, data, jet_name = None, pf_name = None, dR = 0.4):
        '''
        This function match all particle-flow candidates to every jet in the [jet_name] collection
        (function is alternative to match_nearest, but works for multydim case and expected to be much slower)
        '''
        jets = data[jet_name]
        pfcands = data[pf_name]
        # here return_combinations=True is needed to return _pfcands_unzipped
        # which broadcasted in the way to duplicate pdcands list per every jet in event
        (dr, (_, _pfcands_unzipped)) = jets.metric_table(pfcands, metric=coffea.nanoevents.methods.vector.LorentzVector.delta_r, return_combinations=True)
        pfcands_matched = _pfcands_unzipped[(dr < dR)]
        return pfcands_matched
    
    
    @zero_handler
    def get_matched_pfCands(self, data, match_object, dR=0.4):
        pfCands = self.match_jet_to_pfcand(data, jet_name=match_object, pf_name="PfCands", dR=dR)
        pfCands_lead = ak.firsts(pfCands, axis=-1)
        pfCands_lead["dxysig"] = pfCands_lead.dxy / pfCands_lead.dxyError
        pfCands_lead["Lrel"] = np.sqrt(pfCands_lead.dxy**2 + pfCands_lead.dz**2)
        pfCands_lead["dxy_weight"] = ak.mean(pfCands.dxy, weight=pfCands.pt, axis=-1)
        pfCands_lead["dxysig_weight"] = ak.mean(pfCands.dxy / pfCands.dxyError, weight=pfCands.pt, axis=-1)
        return pfCands_lead
    
    @zero_handler
    def set_jet_dxy(self, data):
        jets = data["Jet_select"]
        # Mask jets with dxy nan (no selected pfcands matching)
        bad_jets = ak.is_none(data["Jet_lead_pfcand"].dxy, axis=-1)
        jets = ak.mask(jets, ~bad_jets) # mask bad jets to keep coorect shape
        jets["dz"] = np.abs(data["Jet_lead_pfcand"].dz)
        jets["dxy"] = np.abs(data["Jet_lead_pfcand"].dxy)
        jets["dxy_weight"] = np.abs(data["Jet_lead_pfcand"].dxy_weight)
        jets["dxysig"] = np.abs(data["Jet_lead_pfcand"].dxysig)
        jets["dxysig_weight"] = np.abs(data["Jet_lead_pfcand"].dxysig_weight)
        jets = jets[~bad_jets] # remove bad jets
        return jets

    @zero_handler
    def jet_selection_lead(self, data):
        jets = ak.firsts(data["Jet_select"])
        # print(jets)
        return jets
    
    @zero_handler
    def set_njets_pass(self, data):
        # print("PRINTINT DATA")
        # print(data["Jet_select"].disTauTag_score1)
        jets_score = data["Jet_select"].disTauTag_score1
        n_pass = []
        for score in self.config["score_pass"]:
            jets_pass = jets_score[(jets_score>score)]
            passed = ak.num(jets_pass, axis=1)
            n_pass.append( passed )
        n_pass = ak.from_regular(
            np.stack(n_pass, axis=1), axis=-1)
        return {
            "n_pass" : n_pass,
            "n_pass_score_bin" : ak.local_index(n_pass, axis=1)
        }
        
    @zero_handler
    def predict_yield(self, data, weight=None):
        jets = data["Jet_select"]
        
        # from bin 0 to bin 1
        weights_bin0to1 = []
        for score in self.config["score_pass"][1:]: # skip first bin because it is just 1
            events_0tag = (ak.num(jets[(jets.disTauTag_score1 > score)]) == 0) # events with 0 tag
            jets_notag = (jets.disTauTag_score1 < score) # to have a per jet mask
            jets_counted = jets[events_0tag * jets_notag] # to select only jets in events with 0 tag
            fake_sf =  self.config["jet_fake_rate"](jet_dxy=jets_counted.dxy, jet_pt=jets_counted.pt, jet_score=score)
            weight_sfs = ak.sum(fake_sf, axis=1)
            weights_bin0to1.append(weight_sfs)
        yield_bin0to1 = ak.from_regular(np.stack(weights_bin0to1, axis=1), axis=-1)
        # print(yield_bin0to1)

        # from bin 1 to bin 2
        weights_bin1to2 = []
        for score in self.config["score_pass"][1:]: # skip first bin because it is just 1
            events_1tag = (ak.num(jets[(jets.disTauTag_score1 > score)]) == 1) # events with 1 tag
            jets_notag = (jets.disTauTag_score1 < score) # to have a per jet mask and not to count the tagged jet
            jets_counted = jets[events_1tag * jets_notag]  # to select only jets in events with 1 tag
            fake_sf =  self.config["jet_fake_rate"](jet_dxy=jets_counted.dxy, jet_pt=jets_counted.pt, jet_score=score)
            weight_sfs = ak.sum(fake_sf, axis=1)
            weights_bin1to2.append(weight_sfs)
        yield_bin1to2 = ak.from_regular(np.stack(weights_bin1to2, axis=1), axis=-1)
        # print(yield_bin1to2)
        
        # from bin 0 to bin 2
        weights_bin0to2 = []
        for score in self.config["score_pass"][1:]: # skip first bin because it is just 1
            events_0tag = (ak.num(jets[(jets.disTauTag_score1 > score)]) == 0) # events with 0 tag
            jets_notag = (jets.disTauTag_score1 < score) # to have a per jet mask
            jets_counted = jets[events_0tag * jets_notag] # to select only jets in events with 1 tag
            fake_sf =  self.config["jet_fake_rate"](jet_dxy=jets_counted.dxy, jet_pt=jets_counted.pt, jet_score=score)
            combinations = ak.combinations(fake_sf, 2, axis=1) # to have all possible combinations of 2 jets
            combinations_unzipped = ak.unzip(combinations)
            products = combinations_unzipped[0] * combinations_unzipped[1]
            weight_sfs = ak.sum(products, axis=1)
            weights_bin0to2.append(weight_sfs)
        yield_bin0to2 = ak.from_regular(np.stack(weights_bin0to2, axis=1), axis=-1)
        # print(yield_bin0to2)
        
        # now we need to each predicted yield assign cooresponding score bin
        score_bin = ak.local_index(yield_bin0to1, axis=1) + 1 # +1 because we skip first bin
        # print(score_bin)
        
        return {"yield_bin0to1" : weight*yield_bin0to1,
                "yield_bin1to2" : weight*yield_bin1to2,
                "yield_bin0to2" : weight*yield_bin0to2,
                "score_bin"     : score_bin}