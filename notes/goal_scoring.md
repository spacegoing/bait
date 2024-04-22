# Goal scoring

- Model for scoring goals, used in HTPS, Fringe (TacticZero) and BestFS
 
- Options to train the model:
  - Proof length weighted sum (Polu et al.) (trained from proof attempts)
  - Provable/Unprovable (HTPS), trained with seq2seq over tokens (with soft labels from proof attempts)
  - Score function (Fringe), trained with policy gradient
  - Take into account visit count, scale from 0 to 1 (high VC, no proof -> 0, low VC, proof -> 1, low VC, no proof -> 0.5)

- Problems:
  - Imbalanced data (most goals aren't proved)
    - Positive unlabelled learning ?
  - Hard to evaluate (need to run the prover)
  - Sweep over hyperparameters (exploration constant, visit count cutoffs)

- To compare approaches, the same underlying goal model would be helpful
  - E.g. trained on the same provable/unprovable, used for HTPS, Fringe
  - Need to decide on the best approach for training the model
    - Compare provable/unprovable, proof length, vc scaling, run 1-2 experiments with each 
  - Should we be able to look at a proof trace independent of the approach, and train a model from this?
    - HTPS doesn't, as it uses the model to determine the targets
  

- Paper ideas (5 months to ICLR):
  - If we can improve on HTPS/BestFS use that
  - Otherwise paper would focus on thorough comparison of Fringe, HTPS, BestFS, BFS
  - Would compare training approaches (proof length, provable/unprovable, vc scaling)
  - Further would explore hyperparameter influence (exploration constant, num nodes expanded, allowing dynamic expansion)
  - Investigate meta-controller vs random sampling