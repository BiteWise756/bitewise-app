import re

# Read the recipes from bitewise-app.html
with open('bitewise-app.html', 'r') as f:
    content = f.read()

# Extract the recipes JSON section
start = content.index("const recipes = [") + len('const recipes = [')
end = content.index("];\n  let activeCategory")
recipes_section = content[start:end+1]

# Simple parsing - find each recipe object
recipes = re.findall(r"\{name:'([^']+)', image:'([^']+)', alt:'([^']+)'\", recipes_section)

print('=== Recipes in bitewise-app.html ===')
for name, image, alt in recipes:
    print(f'{name}')
    print(f'  image: {image}')
    print(f'  alt: {alt}')
    print()

# Read the recipes from bitewise-app(2).html
with open('bitewise-app(2).html', 'r') as f:
    content2 = f.read()

# Extract the recipes JSON section
start2 = content2.index("const recipes = [") + len('const recipes = [')
end2 = content2.index("];\n  let activeCategory")
recipes_section2 = content2[start2:end2+1]

# Simple parsing - find each recipe object
recipes2 = re.findall(r"\{name:'([^']+)', image:'([^']+)', alt:'([^']+)'\", recipes_section2")

print('=== Recipes in bitewise-app(2).html ===')
for name, image, alt in recipes2:
    print(f'{name}')
    print(f'  image: {image}')
    print(f'  alt: {alt}')
    print()