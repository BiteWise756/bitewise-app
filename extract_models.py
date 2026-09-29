import pickle, os, json

models_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'models')
output = {}

for model_name, fname in [('breast', 'breast_cancer.pkl'), ('leukemia', 'leukemia_model.pkl'), ('liver', 'liver_model.pkl'), ('prostate', 'prostate_model.pkl')]:
    with open(os.path.join(models_dir, fname), 'rb') as f:
        model = pickle.load(f)
    
    booster = model.get_booster()
    dumps = booster.get_dump(dump_format='json')
    trees_json = [json.loads(d) for d in dumps]
    
    # Get base score from the model
    base_score = 0.5
    try:
        learner_str = booster.attr('learner')
        if learner_str:
            learner_attr = json.loads(learner_str)
            base_score = float(learner_attr.get('learner', {}).get('learning_rate_params', {}).get('base_score', 0.5))
    except:
        pass

    output[model_name] = {
        'type': type(model).__name__,
        'n_features': model.n_features_in_,
        'n_estimators': len(trees_json),
        'feature_names': booster.feature_names,
        'base_score': base_score,
        'trees': trees_json,
    }

with open(os.path.join(models_dir, 'models_data.json'), 'w') as f:
    json.dump(output, f)

print('Models extracted to models_data.json')
for name, data in output.items():
    print(f'  {name}: {data["n_estimators"]} trees, {data["n_features"]} features')
total_size = os.path.getsize(os.path.join(models_dir, 'models_data.json'))
print(f'Total JSON size: {total_size} bytes ({total_size/1024:.1f} KB)')
